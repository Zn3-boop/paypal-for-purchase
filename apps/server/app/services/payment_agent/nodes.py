"""支付子图节点 —— 授权边界主线（在现有 LangGraph 上插入）。

契约五节点的落地实现：

  1. parse_intent      LLM 解析"帮我买个适合周五晚上听的唱片" -> {品类,预算,场景}
  2. taste_select      调 shopper.rank_candidates（L1 Channel3 真实搜索 -> 降级 L2）
  3. authority_check   授权判定：信任分 + 长期目标 + 行为信号
  4. approval_gate     interrupt_before 挂起，用"为什么需要你确认"做审批上下文
  5. paypal_create     Orders v2 create (intent=CAPTURE)
  6. paypal_capture    capture + 落 decision trace
  7. explain           生成事后解释 + 回写信任分（A/B 级证据闸门）

约束（对齐契约红线）：
  - fixture 冻结的是商品"数据"，不是"判断"：意图解析/推荐理由/授权理由/
    事后解释每轮都真实生成，状态里显式记录 llm_source。
  - authority_check = 规则算边界 + LLM 写理由 的混合。
  - 信任分回写必须走 evidence.apply_evidence（A/B 级闸门）。
  - 失败绝不硬失败：任何单点异常降级空结果。

本模块按需懒加载外部增强能力，缺失即降级。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.services.payment_agent import evidence as eve
from app.services.payment_agent.goals import GoalMemory, SavingGoal, get_goal_memory
from app.services.payment_agent.impulse import (
    PurchaseSignal,
    check_impulse,
)
from app.services.shopper.catalog import get_catalog_items
from app.services.shopper.ranker import rank_candidates

logger = logging.getLogger(__name__)

# .env 由 app.core.config.Settings 加载但不会注入 os.environ：LLM_MODE / PAYMENT_MODE 从 Settings 兜底
from app.core.config import get_settings as _get_settings  # noqa: E402

LLM_MODE = os.getenv("LLM_MODE") or _get_settings().llm_mode
PAYMENT_MODE = os.getenv("PAYMENT_MODE") or _get_settings().payment_mode  # mock / real
DEFAULT_BUDGET = float(os.getenv("PAYMENT_DEFAULT_BUDGET", "50.0"))
MONTHLY_BUDGET = float(os.getenv("PAYMENT_MONTHLY_BUDGET", "150.0"))
# P2-10 收尾：real 路径的 paypal_source 标记（sandbox_real / live_real），
# 前端按 source 显示来源徽标，mock 路径固定 "mock"。
PAYPAL_ENV = (
    "sandbox"
    if "sandbox" in os.getenv("PAYPAL_SANDBOX_API", "https://api-m.sandbox.paypal.com")
    else "live"
)
PAYPAL_REAL_SOURCE = f"{PAYPAL_ENV}_real"


def _gen_intent_id(category: str) -> str:
    """生成稳定意图 ID（时间戳前缀, 每轮真实生成）。"""
    import time

    category_tag = (category or "merch")[:8]
    return f"intent_{int(time.time() * 1000)}_{category_tag}"


# ============================================================
# 节点 1：parse_intent —— LLM 解析购买意图
# ============================================================

async def parse_intent_node(state: dict[str, Any]) -> dict[str, Any]:
    """LLM 解析购买意图 -> shopping_intent（每次真实生成，不冻结）。

    产出 shopping_intent= {
      category, budget, scene, keywords, llm_source(real|mock_rule), llm_hint
    }
    """
    raw = state.get("raw_query", "")
    if not raw:
        return {"shopping_intent": {}, "shopping_error": "empty_query"}
    intent = await _llm_parse_intent(raw)
    logger.info(
        "parse_intent: category=%s budget=%s scene=%s llm_source=%s",
        intent.get("category"), intent.get("budget"), intent.get("scene"),
        intent.get("llm_source"),
    )
    return {"shopping_intent": intent, "shopping_error": ""}


async def _llm_parse_intent(raw_query: str) -> dict[str, Any]:
    """LLM 解析购买意图；LLM_MODE=real 时真调 LLM，否则规则近似。

    两种模式都产出结构化 intent —— 判断是真实的，只是来源不同。

    P2-10 修复：此前误调 plan_steps_llm（多步 Agent 规划专用），参数
    task_goals 不存在 + 返回 (is_multi, steps) 元组被当 list 用，两处
    TypeError 都被 except 吞掉导致真实 LLM 永不生效。现改为专用
    ShoppingIntentOutput + llm_parse_with_retry，与 _llm_write_authority_reason
    同一范本。
    """
    if LLM_MODE == "real":
        try:
            from pydantic import BaseModel, Field

            from app.services.llm.client import llm_parse_with_retry

            class ShoppingIntentOutput(BaseModel):
                category: str = Field(description="商品品类: music/dining/film/donation/merch")
                budget: float = Field(ge=0, le=100000, description="用户预算（USD）")
                scene: str = Field(description="消费场景一句话")
                keywords: list[str] = Field(description="品味的锚点词，1-5个")

            parsed, _ = await llm_parse_with_retry(
                prompt=(
                    "你是支付代理，从用户一句话里提取购买意图，只输出 JSON。\n"
                    "category 必须严格从下面枚举里选一个：\n"
                    "  music  (唱片/黑胶/爵士/演唱会/歌手)\n"
                    "  dining (餐厅/美食/咖啡/意餐)\n"
                    "  film   (电影/影片/蓝光)\n"
                    "  donation (捐款/捐赠/慈善)\n"
                    "  merch  (其他商品)\n"
                    "示例：\n"
                    "  用户：帮我买一张周五听的爵士唱片\n"
                    "  输出：{\"category\":\"music\",\"budget\":50,\"scene\":\"周五夜晚放松\",\"keywords\":[\"爵士\",\"唱片\"]}\n"
                    "  用户：给我订一家附近的意餐\n"
                    "  输出：{\"category\":\"dining\",\"budget\":50,\"scene\":\"用餐\",\"keywords\":[\"意餐\",\"餐厅\"]}\n"
                    "现在处理：\n"
                    f"  用户：{raw_query}\n"
                    "只输出 JSON，不要解释。"
                ),
                resp_model=ShoppingIntentOutput,
            )
            if parsed is not None and parsed.category:
                cat = _normalize_category(parsed.category, raw_query)
                return {
                    "category": cat,
                    "budget": float(parsed.budget),
                    "scene": parsed.scene,
                    "keywords": parsed.keywords or [cat],
                    "raw_query": raw_query,
                    "llm_source": "real",
                    "llm_hint": parsed.scene,
                }
        except Exception as exc:  # pragma: no cover - 降级不硬失败
            logger.warning("parse_intent LLM 调用失败，降级规则: %s", exc)
    parsed = _rule_parse_intent(raw_query)
    parsed["llm_source"] = "mock_rule"
    parsed["llm_hint"] = ""
    return parsed


def _normalize_category(category: str, raw_query: str = "") -> str:
    """白名单归一化：模型可能返回枚举外的 category（qwen-flash 实测会）。

    策略：已知别名映射 -> 关键词重判 -> 兜底 merch。保证 category 永远在
    music/dining/film/donation/merch 枚举内，避免错误品类流进推荐链。
    """
    cat = (category or "").strip().lower()
    # 精确别名（确定性高，不会误伤）
    alias = {
        "music": "music",
        "vinyl": "music",
        "album": "music",
        "jazz": "music",
        "restaurant": "dining",
        "dining": "dining",
        "food": "dining",
        "coffee": "dining",
        "movie": "film",
        "film": "film",
        "cinema": "film",
        "donation": "donation",
        "charity": "donation",
        "merch": "merch",
    }
    if cat in alias:
        return alias[cat]
    # 泛化词：模型对"买X"这类表述常返回 purchase/payment/goods/shopping（不携带品类信息），
    # 这些绝不能提前兜底为 merch —— 必须落到关键词重判（qwen-flash 实测对"买爵士唱片"返回
    # purchase，直接映射会误判品类，绕过 music 关键词）。
    # 关键词重判（与 _rule_parse_intent 对齐）
    q = raw_query.lower()
    if any(kw in q for kw in ("唱片", "vinyl", "黑胶", "爵士", "音乐", "歌手", "演唱会", "专辑")):
        return "music"
    if any(kw in q for kw in ("意餐", "餐厅", "美食", "dining", "套餐", "点心", "咖啡")):
        return "dining"
    if any(kw in q for kw in ("电影", "影片", "蓝光", "film", "movie", "4k")):
        return "film"
    if any(kw in q for kw in ("捐款", "捐赠", "donation", "爱心", "慈善")):
        return "donation"
    return "merch"


def _rule_parse_intent(raw_query: str) -> dict[str, Any]:
    """规则近似提取：尽量从 query 里捞结构化意图，兜底默认值。"""
    import re

    q = raw_query.lower()
    category = "merch"
    budget = DEFAULT_BUDGET
    scene = "日常"

    if any(kw in q for kw in ("唱片", "vinyl", "黑胶", "爵士", "音乐", "歌手", "演唱会")):
        category = "music"
    elif any(kw in q for kw in ("意餐", "餐厅", "美食", "dining", "套餐", "点心")):
        category = "dining"
    elif any(kw in q for kw in ("电影", "影片", "蓝光", "film", "movie", "4k")):
        category = "film"
    elif any(kw in q for kw in ("捐款", "捐赠", "donation", "爱心")):
        category = "donation"
    if "周五" in q or "friday" in q or "星期五" in q:
        scene = "周五夜晚放松"

    m = re.search(r"(\d[\d,.]*)", q)
    if m:
        try:
            budget = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    keywords = [kw for kw in ("爵士", "黑胶", "意餐", "低钠", "捐款", "蓝光")
                if kw in raw_query]
    if not keywords:
        keywords = [category]
    return {
        "category": category,
        "budget": budget,
        "scene": scene,
        "keywords": keywords,
        "raw_query": raw_query,
    }


# ============================================================
# 节点 2：taste_select —— 候选商品选择
# ============================================================

async def taste_select_node(state: dict[str, Any]) -> dict[str, Any]:
    """候选商品选择：L1 Channel3 真实搜索 -> 降级 L2 本地排序。

    永远有输出（catalog 冻结 + ranker 本地），Channel3 只是增强层。
    """
    intent = state.get("shopping_intent", {}) or {}
    category = intent.get("category", "merch")
    try:
        budget = float(intent.get("budget", DEFAULT_BUDGET) or DEFAULT_BUDGET)
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET
    keywords = intent.get("keywords", []) or []

    # 品味锚点：关键词 + 会话带入的品味实体
    anchors: list[str] = list(keywords)
    entities = state.get("taste_entities", []) or []
    anchors.extend([str(e) for e in entities[:3]])

    rank_source = "local_ranker"
    candidates: list[dict[str, Any]] = []
    try:
        # L1：Channel3 真实商品搜索（有 key 才执行，失败自动降级 L2）
        from app.services.channel3 import search_products_for_query

        query = " ".join(keywords) if keywords else category
        channel3_items = await search_products_for_query(query, limit=8)
        if channel3_items:
            candidates = channel3_items
            rank_source = "channel3"
    except Exception:  # pragma: no cover - Channel3 挂了不影响本节点
        logger.warning("Channel3 增强失败，降级 L2 本地 catalog")
        candidates = []

    if not candidates:
        candidates = get_catalog_items()
        rank_source = "local_ranker"

    ranked = rank_candidates(
        candidates=candidates,
        taste_anchors=anchors,
        preferred_categories=[category],
        budget=budget,
    )
    if not ranked:
        return {"shopping_candidates": [], "shopping_rank_source": "local_ranker"}

    return {
        "shopping_candidates": ranked,
        "shopping_intent_id": _gen_intent_id(category),
        "shopping_selected": ranked[0],
        "shopping_rank_source": rank_source,
        "shopping_anchors": anchors,
        "shopping_budget": budget,
    }


# ============================================================
# 节点 3：authority_check —— 规则算边界 + LLM 写理由
# ============================================================

async def authority_check_node(state: dict[str, Any]) -> dict[str, Any]:
    """授权判定：信任分 + 长期目标 + 行为信号 的混合裁决。

    输出 authority = {
      verdict: allow_auto | need_approval | block,
      reason, trust_score, rule, risk_level,
      goal_conflicts: [...], impulse: {...}, llm_reason
    }
    """
    user_id = state.get("user_id", "anonymous")
    intent = state.get("shopping_intent", {}) or {}
    selected = state.get("shopping_selected", {}) or {}
    try:
        amount = float(selected.get("price", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    category = selected.get("category", intent.get("category", "merch"))

    # --- 信任画像（A/B 级证据演化出的 trust_profile） ---
    from app.services.paypal.authz import ProgressiveAuthzEngine, TrustProfile

    trust_raw = state.get("trust_profile")
    trust: TrustProfile = (
        trust_raw if isinstance(trust_raw, TrustProfile) else TrustProfile()
    )

    # --- 目标约束记忆 ---
    goal_mem = state.get("goal_memory")
    if goal_mem is None:
        goal_mem = get_goal_memory(state.get("session_id"))
    goal_conflicts = goal_mem.check_purchase(
        user_id,
        category=category,
        amount=amount,
        monthly_spent=float(state.get("monthly_spent", 0.0) or 0.0),
    )

    # --- 冲动消费拦截（行为信号版，无摄像头） ---
    impulse = check_impulse(
        item_name=selected.get("name", ""),
        amount=amount,
        monthly_spent=float(state.get("monthly_spent", 0.0) or 0.0),
        monthly_budget=MONTHLY_BUDGET,
        savings_conflict=goal_conflicts,
        history=state.get("purchase_history", []) or [],
    )

    # --- 规则算边界：渐进式授权引擎 ---
    decision = ProgressiveAuthzEngine.decide(
        amount=amount,
        trust=trust,
        blocked_rules=goal_conflicts,
        currency=selected.get("currency", "USD"),
    )

    # --- 混合裁决 ---
    if impulse.triggered:
        verdict = "block"
        reason = f"【冲动拦截】{impulse.reason}（信任分 {trust.trust_score:.2f}）"
    elif decision.allow_auto:
        verdict = "allow_auto"
        reason = decision.reason
    else:
        verdict = "need_approval"
        reason = decision.reason

    llm_reason, llm_source = await _llm_write_authority_reason(
        verdict=verdict,
        item=selected,
        amount=amount,
        trust_score=trust.trust_score,
        goal_conflicts=goal_conflicts,
        impulse_signals=impulse.signals,
    )

    return {
        "authority": {
            "verdict": verdict,
            "reason": reason,
            "llm_reason": llm_reason,
            "trust_score": trust.trust_score,
            "rule": decision.rule,
            "risk_level": "high" if verdict == "block" else (
                "low" if verdict == "allow_auto" else "medium"
            ),
            "goal_conflicts": goal_conflicts,
            "impulse": {
                "triggered": impulse.triggered,
                "signals": impulse.signals,
            },
            # 诚实性：llm_source 必须是"本次 LLM 调用是否真实成功"的标记，
            # 不能等于配置模式 LLM_MODE —— LLM 失败回落规则时前端徽章必须显示
            # mock_rule，禁止把规则理由伪装成真实 LLM 产出。
            "llm_source": llm_source,
        },
        "shopping_amount": amount,
        "shopping_category": category,
        "goal_conflicts": goal_conflicts,
        "impulse_result": {
            "triggered": impulse.triggered,
            "signals": impulse.signals,
        },
    }


async def _llm_write_authority_reason(
    *,
    verdict: str,
    item: dict[str, Any],
    amount: float,
    trust_score: float,
    goal_conflicts: list[str],
    impulse_signals: list[str],
) -> tuple[str, str]:
    """LLM 写授权理由（真实生成，不冻结）。失败时用规则拼接兜底。

    返回 (理由文本, llm_source)：
      - (content.reason, "real")      真实 LLM 产出
      - (base, "mock_rule")           LLM 失败 / 配置为 mock 时的规则兜底
    llm_source 用真实调用结果标记，绝不把规则理由伪装成 real。
    """
    verdict_label = {
        "allow_auto": "自动放行",
        "need_approval": "需要你确认",
        "block": "拦截",
    }.get(verdict, verdict)
    base = (
        f"代理建议{verdict_label}「{item.get('name', '')}」"
        f"（{amount:.2f} {item.get('currency', 'USD')}）。"
        f"当前信任分 {trust_score:.2f}。"
    )
    if goal_conflicts:
        base += "命中目标约束：" + "；".join(goal_conflicts[:2]) + "。"
    if impulse_signals:
        base += "触发冲动拦截信号：" + "；".join(impulse_signals[:2]) + "。"

    if LLM_MODE == "real":
        try:
            from app.services.llm.client import llm_parse_with_retry
            from pydantic import BaseModel, Field

            class ReasonModel(BaseModel):
                reason: str = Field(min_length=10, max_length=600)

            content, _ = await llm_parse_with_retry(
                prompt=(
                    "你是一个可审计的支付代理。请用一句不超过80字的中文，"
                    "向用户解释为什么这笔支付被" + verdict_label + "。"
                    "背景：" + base
                ),
                resp_model=ReasonModel,
            )
            if content and content.reason:
                return content.reason, "real"
        except Exception as exc:  # pragma: no cover - 降级不硬失败
            logger.warning("authority LLM 理由生成失败，用规则兜底: %s", exc)
    return base, "mock_rule"


# ============================================================
# 节点 4：approval_gate —— HITL 审批闸门（复用 interrupt_before）
# ============================================================

def approval_gate_node(state: dict[str, Any]) -> dict[str, Any]:
    """审批闸门：图会在进入此节点前 interrupt_before。

    state['approval_decision'] 由外部 API 写入：
      approve  -> 放行（status=approved，回写 A 级证据）
      reject   -> 终止（status=cancelled，回写 A 级负证据）
      modify   -> 改金额后放行（status=approved，A 级证据 + 金额修正）
    未决策时返回空（等待中断恢复）。
    """
    authority = state.get("authority", {}) or {}
    decision = state.get("approval_decision", "")
    intent_id = state.get("shopping_intent_id", "")

    if not decision:
        # interrupt 恢复后（状态已含 approval_decision）才继续
        return {}

    if decision == "approve":
        evidence_applied, new_trust = _apply_approval_evidence(
            state, intent_id=intent_id, approved=True
        )
        return {
            "approval_status": "approved",
            "approval_comment": state.get("approval_comment", ""),
            "approval_evidence_applied": evidence_applied,
            "trust_profile": new_trust,
        }
    if decision == "reject":
        evidence_applied, new_trust = _apply_approval_evidence(
            state, intent_id=intent_id, approved=False
        )
        return {
            "approval_status": "cancelled",
            "approval_comment": state.get("approval_comment", ""),
            "approval_evidence_applied": evidence_applied,
            "trust_profile": new_trust,
        }
    if decision == "modify":
        evidence_applied, new_trust = _apply_approval_evidence(
            state, intent_id=intent_id, approved=True, modified=True
        )
        return {
            "approval_status": "approved",
            "approval_comment": state.get("approval_comment", ""),
            "approval_evidence_applied": evidence_applied,
            "trust_profile": new_trust,
        }
    logger.warning("approval_gate 收到未知决策: %r", decision)
    return {"approval_status": "pending"}


def _apply_approval_evidence(
    state: dict[str, Any], *, intent_id: str, approved: bool, modified: bool = False
) -> tuple[dict[str, Any], Any]:
    """A 级证据回写信任分（走 evidence 闸门，从 state 取出当前画像）。

    返回 (evidence_applied 摘要, 更新后的 TrustProfile)——调用方把新画像
    写回 state["trust_profile"]，使 A 级证据真正进入会话内信任演化链。
    """
    from app.services.payment_agent.evidence import apply_evidence

    trust_raw = state.get("trust_profile")
    trust = trust_raw if trust_raw is not None else _default_trust()
    claim = "用户批准支付" if approved else "用户否决支付"
    if modified:
        claim = "用户修改金额后批准支付"
    evidence = eve.build_user_decision_evidence(
        _dummy_intent(intent_id, state.get("user_id", "")),
        decision="approve" if approved else "reject",
        comment=state.get("approval_comment", ""),
    )
    new_trust, _ = apply_evidence(trust, evidence=evidence)
    return (
        {"new_trust_score": new_trust.trust_score, "claim": claim},
        new_trust,
    )


def _dummy_intent(intent_id: str, user_id: str):
    """构造最小 PaymentIntent 供证据构造使用（金额/描述来自 state）。"""
    from app.services.paypal.config import PaymentIntent

    return PaymentIntent(
        id=intent_id or "pending",
        user_id=user_id or "anonymous",
        description="agentic payment",
        amount="0.00",
    )


def _default_trust():
    from app.services.paypal.authz import TrustProfile

    return TrustProfile()


# ============================================================
# 节点 5：paypal_create —— Orders v2 create
# ============================================================

async def paypal_create_node(state: dict[str, Any]) -> dict[str, Any]:
    """Orders v2 create（intent=CAPTURE），产出 order_id + approval_url。

    PAYMENT_MODE=mock 时用假订单；=real 时调 PayPalPaymentService。
    """
    selected = state.get("shopping_selected", {}) or {}
    authority = state.get("authority", {}) or {}
    try:
        amount = float(selected.get("price", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    description = (
        f"品味驱动购买：{selected.get('name', '')}"
        f"（{authority.get('verdict', '')}）"
    )
    intent_id = state.get("shopping_intent_id", "")

    if PAYMENT_MODE == "real":
        try:
            from app.services.paypal.service import (
                PayPalPaymentService,
                get_payment_service,
            )

            svc: PayPalPaymentService = state.get("payment_service") or get_payment_service()
            intent = svc.propose_payment(
                user_id=state.get("user_id", "anonymous"),
                description=description,
                amount=f"{amount:.2f}",
                rationale=authority.get("reason", ""),
                evidence={
                    "match_tags": selected.get("match_tags", []),
                    "rank_source": state.get("shopping_rank_source", ""),
                    "verdict": authority.get("verdict", ""),
                },
            )
            intent, _ = svc.authorize_payment(intent.id)
            # 回跳落地页必须是前端页面，不能是 example.com 死链：
            # 该链路真正的 capture 由图内 paypal_capture_node 完成，因此回跳
            # 指向复核页而不是 callback 端点，避免同一订单被二次 capture。
            frontend = (_get_settings().frontend_base_url or "").rstrip("/")
            session_id = state.get("session_id", "") or ""
            intent, msg = await svc.create_payment_order(
                intent.id,
                return_url=f"{frontend}/payment/review?session_id={session_id}&status=returned",
                cancel_url=f"{frontend}/payment/review?session_id={session_id}&status=cancelled",
            )
            # P2-10 修复（双字段隔离）：
            #   - shopping_intent_id 保持业务 intent 稳定 id（选品阶段，全程不变，
            #     供证据面板/留痕/重试恢复反查使用）；
            #   - paypal_intent_id 是 service 在支付阶段生成的 uuid，create 时
            #     真实 PayPal 订单挂在它名下，capture 节点只认它。
            # 此前 `intent_id or intent.id` 短路保留旧 id -> capture 拿旧 id 在
            # service._intents（uuid key）里查不到 -> KeyError 被吞 -> 静默降级
            # mock capture_id，造成"真实订单 + mock 捕获"人格分裂。
            return {
                "shopping_intent_id": intent_id or "",
                "paypal_intent_id": intent.id,
                "paypal_order_id": intent.paypal_order_id,
                "approval_url": intent.approval_url,
                "paypal_status": intent.status,
                "paypal_msg": msg,
                "paypal_mode": "real",
                "paypal_source": PAYPAL_REAL_SOURCE,
            }
        except Exception as exc:  # pragma: no cover - 降级不硬失败，但保持诚实
            # real 失败不得回落到"假订单"：真实 intent + mock order 比失败更糟。
            logger.warning("paypal_create real 失败，返回失败态（不降级 mock）: %s", exc)
            return {
                "shopping_intent_id": intent_id or "",
                "paypal_intent_id": "",
                "paypal_order_id": "",
                "approval_url": "",
                "paypal_status": "failed",
                "paypal_msg": f"PayPal 订单创建失败：{exc}",
                "paypal_mode": "real",
                "paypal_source": PAYPAL_REAL_SOURCE,
            }

    return {
        "shopping_intent_id": intent_id or "intent-mock-1",
        "paypal_intent_id": "intent-mock-1",
        "paypal_order_id": "ORDER-MOCK-123",
        "approval_url": "https://sandbox.approve/MOCK",
        "paypal_status": "created",
        "paypal_msg": "（mock）PayPal 订单已创建",
        "paypal_mode": "mock",
        "paypal_source": "mock",
    }


# ============================================================
# 节点 6：paypal_capture
# ============================================================

async def paypal_capture_node(state: dict[str, Any]) -> dict[str, Any]:
    """capture 已审批订单 + 落 decision trace（含授权理由/选品来源）。

    P2-10 加固（工单）：
      - 幂等：paypal_status 已是 captured -> 直接返回已有回执，不重复调 PayPal；
      - 只认 paypal_intent_id（支付阶段 service uuid），不再把业务 shopping_intent_id
        当 service key 用（旧 id 查不到 -> KeyError -> 静默降级 mock 是人格分裂根源）；
      - real 失败 / intent 未命中 -> 显式 capture_failed + evidence_caveats 诚实标注，
        禁止回落 "CAP-MOCK-*" 假成功；
      - mock 仅由 PAYPAL_MODE=mock 开关触发，结果带 paypal_source=mock 标记。
    """
    order_id = state.get("paypal_order_id", "")
    authority = state.get("authority", {}) or {}
    selected = state.get("shopping_selected", {}) or {}
    existing_status = state.get("paypal_status", "")
    existing_capture_id = state.get("capture_id", "")

    def _trace(capture_id: str, status: str) -> dict[str, Any]:
        """P2-8：决策留痕补齐 reason/llm_reason/shop_source —— 让 trace 自解释。
        P2-10 收尾：trace 补 source 字段（与响应 payment.source 同源），
        供一致性断言（真实 order + mock source 必须当场失败）。
        """
        return {
            "order_id": order_id,
            "capture_id": capture_id,
            "status": status,
            "item_name": selected.get("name", ""),
            "reason": authority.get("reason", ""),
            "llm_reason": authority.get("llm_reason", ""),
            "shop_source": state.get("shopping_rank_source", ""),
            "source": state.get(
                "paypal_source",
                PAYPAL_REAL_SOURCE if PAYMENT_MODE == "real" else "mock",
            ),
            "ts": _utc_now(),
        }

    # ---- 幂等：已经成功捕获过就不重复扣款 ----
    if existing_status == "captured" and existing_capture_id:
        logger.info(
            "capture 幂等命中：order=%s capture=%s 已捕获，跳过重复 capture",
            order_id, existing_capture_id,
        )
        return {
            "paypal_status": existing_status,
            "capture_id": existing_capture_id,
            "paypal_msg": state.get("paypal_msg", "") or "该订单已捕获",
            "decision_trace": _trace(existing_capture_id, existing_status),
            "paypal_mode": state.get("paypal_mode", "real" if PAYMENT_MODE == "real" else "mock"),
            "paypal_source": state.get(
                "paypal_source",
                PAYPAL_REAL_SOURCE if PAYMENT_MODE == "real" else "mock",
            ),
        }

    if PAYMENT_MODE == "real":
        caveats: list[str] = []
        intent_id = state.get("paypal_intent_id", "")
        if not intent_id:
            caveats.append("缺少 paypal_intent_id，无法定位待捕获订单")
            return {
                "paypal_status": "capture_failed",
                "capture_id": "",
                "paypal_msg": "捕获失败：缺少支付订单标识",
                "paypal_error": "；".join(caveats),
                "evidence_caveats": caveats,
                "decision_trace": _trace("", "capture_failed"),
                "paypal_mode": "real",
                "paypal_source": PAYPAL_REAL_SOURCE,
            }
        try:
            from app.services.paypal.service import get_payment_service

            svc = state.get("payment_service") or get_payment_service()
            existing = svc.get_intent(intent_id)
            if existing is None:
                caveats.append(f"service 内不存在 intent={intent_id}，订单可能未创建或已被消费")
                logger.warning("paypal_capture: intent 未命中 %s", intent_id)
                return {
                    "paypal_status": "capture_failed",
                    "capture_id": "",
                    "paypal_msg": "捕获失败：找不到对应订单",
                    "paypal_error": "；".join(caveats),
                    "evidence_caveats": caveats,
                    "decision_trace": _trace("", "capture_failed"),
                    "paypal_mode": "real",
                    "paypal_source": PAYPAL_REAL_SOURCE,
                }
            intent, msg = await svc.capture_payment_order(intent_id)
            # 语义断言（防火墙）：real 模式禁止 mock capture id 混入
            cid = intent.capture_id or ""
            if cid.startswith("CAP-MOCK"):
                caveats.append("service 返回了 mock capture id（不应发生），按失败处理")
                logger.error("paypal_capture: real 模式收到 mock capture_id=%s", cid)
                return {
                    "paypal_status": "capture_failed",
                    "capture_id": "",
                    "paypal_msg": "捕获失败：收到异常捕获标识",
                    "paypal_error": "；".join(caveats),
                    "evidence_caveats": caveats,
                    "decision_trace": _trace("", "capture_failed"),
                    "paypal_mode": "real",
                    "paypal_source": PAYPAL_REAL_SOURCE,
                }
            if intent.status == "captured":
                return {
                    "paypal_status": "captured",
                    "capture_id": cid,
                    "paypal_msg": msg,
                    "decision_trace": _trace(cid, "captured"),
                    "paypal_mode": "real",
                    "paypal_source": PAYPAL_REAL_SOURCE,
                }
            # 未捕获（如 ORDER_NOT_APPROVED / 失败）-> 显式失败态，不返回假成功
            caveats.append(f"capture 未完成：{msg}")
            return {
                "paypal_status": "capture_failed",
                "capture_id": "",
                "paypal_msg": msg,
                "paypal_error": "；".join(caveats),
                "evidence_caveats": caveats,
                "decision_trace": _trace("", "capture_failed"),
                "paypal_mode": "real",
                "paypal_source": PAYPAL_REAL_SOURCE,
            }
        except Exception as exc:  # pragma: no cover - 不硬失败，但拒绝假成功
            caveats.append(f"capture 异常：{exc}")
            logger.warning("paypal_capture real 异常，标记 capture_failed（不降级 mock）: %s", exc)
            return {
                "paypal_status": "capture_failed",
                "capture_id": "",
                "paypal_msg": f"捕获失败：{exc}",
                "paypal_error": "；".join(caveats),
                "evidence_caveats": caveats,
                "decision_trace": _trace("", "capture_failed"),
                "paypal_mode": "real",
                "paypal_source": PAYPAL_REAL_SOURCE,
            }

    # ---- mock 分支（仅 PAYMENT_MODE=mock 显式触发，带来源标记）----
    return {
        "paypal_status": "captured",
        "capture_id": "CAP-MOCK-1",
        "paypal_msg": "（mock）订单已捕获",
        "decision_trace": _trace("CAP-MOCK-1", "captured"),
        "paypal_mode": "mock",
        "paypal_source": "mock",
    }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 节点 7：explain —— 事后解释 + 信任分回写（A/B 级证据闸门）
# ============================================================

async def explain_node(state: dict[str, Any]) -> dict[str, Any]:
    """生成事后解释 + 回写信任分（走 A/B 级证据闸门）。

    产出：
      explanation: {text, llm_source, structured}
      trust_updated: {old_score, new_score, evidence_grade, evidence_claim}
    """
    selected = state.get("shopping_selected", {}) or {}
    authority = state.get("authority", {}) or {}
    paypal_status = state.get("paypal_status", "")

    trust_raw = state.get("trust_profile")
    trust = trust_raw if trust_raw is not None else _default_trust()
    intent = _dummy_intent(state.get("shopping_intent_id", ""), state.get("user_id", ""))
    intent.description = f"品味驱动购买：{selected.get('name', '')}"

    # B 级证据：订单结果事实（captured=成功，否则失败）
    evidence = eve.build_order_outcome_evidence(
        intent, succeeded=(paypal_status == "captured")
    )
    from app.services.payment_agent.evidence import apply_evidence

    new_trust, _ = apply_evidence(trust, evidence=evidence)

    explanation = await _llm_write_explanation(
        item=selected,
        authority=authority,
        paypal_status=paypal_status,
        new_trust_score=new_trust.trust_score,
    )

    return {
        "explanation": explanation,
        "trust_updated": {
            "old_score": trust.trust_score,
            "new_score": new_trust.trust_score,
            "evidence_grade": evidence.grade.value,
            "evidence_claim": evidence.claim,
        },
        # 写回信任画像：B 级订单证据后的最新画像进入会话态（P0-3 跨会话持久化取这里）
        "trust_profile": new_trust,
    }


async def _llm_write_explanation(
    *,
    item: dict[str, Any],
    authority: dict[str, Any],
    paypal_status: str,
    new_trust_score: float,
) -> dict[str, Any]:
    """LLM 生成事后解释（真实生成）；失败用结构化规则兜底。"""
    verdict = authority.get("verdict", "")
    base = (
        f"这笔支付（{item.get('name', '')}）的裁决是 {verdict}，"
        f"支付状态 {paypal_status}。信任分现为 {new_trust_score:.2f}。"
    )
    if LLM_MODE == "real":
        try:
            from app.services.llm.client import llm_parse_with_retry
            from pydantic import BaseModel, Field

            class ExplainModel(BaseModel):
                text: str = Field(min_length=10, max_length=800)

            content, _ = await llm_parse_with_retry(
                prompt=(
                    "你是可审计支付代理。用不超过100字向用户解释刚才这笔支付"
                    "的完整链路（为什么选它、为什么授权、结果如何）。" + base
                ),
                resp_model=ExplainModel,
            )
            if content and content.text:
                return {"text": content.text, "llm_source": "real"}
        except Exception as exc:  # pragma: no cover
            logger.warning("explain LLM 失败，用规则兜底: %s", exc)
    return {
        "text": base,
        "llm_source": "mock_rule",
        "structured": {
            "item": item.get("name", ""),
            "verdict": verdict,
            "paypal_status": paypal_status,
            "match_tags": item.get("match_tags", []),
        },
    }