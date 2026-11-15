"""检索/重排策略消融（真网返回 + 判定函数当裁判，2026-11-14）。

对标「设计检索策略对照实验」的量化证据。与质量门禁同一批评测 query
（quality_gate.SCENARIOS，本次快照采样 20 句基线；cache 重刷即可扩到 30），
把「检索/重排」拆成两个环节，本脚本消融**重排（本地可控）**一环：

  □ 对照组   topk_raw  候选原序取 k（模拟 Channel3 返回顺序，无重排）
  □ 实验组 1 greedy    按相关度分排序取 k（打分排序，无多样性抑制）
  □ 实验组 2 mmr_l*    rel 归一化 + MMR，λ ∈ {0.3, 0.5, 0.7, 1.0} 扫描

**指标完全基于项目自己的判定函数**（`satisfaction.evaluate`），不含任何
人工标注的 ground truth —— 这是本脚本与「自造数据集刷 Recall」的本质区别：

  - Hit@k      = 前 k 个商品里存在至少一个「无任何 ❌ 判定」（逐件 evaluate）
  - MRR        = 第一个「无 ❌」商品按策略排序后的位置倒数（无则 0）
  - 可判定率   = 列表级 evaluate 的 (sat+unsat) / 全约束数（非 unknown 占比）
  - 品类多样性 = top-k 的唯一 category 数（MMR 的本职）
  - 价格 CV    = top-k 价格 std/mean（结果是否同质化）

诚实边界（写进结论）：
  - 检索是外部 API 黑盒，不重放、不 mock；候选池来自一次真实搜索（limit=20）。
  - 消融的只是「搜索之后、判定之前」的重排环节，因为那是本地可控的。
  - 指标是「判定层看到的」质量，不是「用户主观的」质量 —— 和数据一致。

缓存：`--cache out/live.json` 把真网返回落盘；`--offline` 重放缓存，
演示时 3 秒复算、断网可用（与 satisfaction_golden 同哲学）。

用法：
    python scripts/retrieval_ablation.py --cache docs/retrieval_live.json
    python scripts/retrieval_ablation.py --offline --cache docs/retrieval_live.json --k 3
    python scripts/retrieval_ablation.py --json docs/retrieval_ablation.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，报告含中文会炸；切 UTF-8 再输出。
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "server"))
sys.path.insert(0, str(ROOT))

from app.services.group_purchase.mmr import minmax, mmr_select  # noqa: E402
from app.services.group_purchase.planner import _query_tokens, build_search_query  # noqa: E402
from app.services.group_purchase.satisfaction import evaluate  # noqa: E402
from app.services.group_purchase.tiering import similarity  # noqa: E402
from scripts.quality_gate import (  # noqa: E402
    SCENARIOS,
    _prefilter_by_budget,
    mk_constraints,
)

DEFAULT_K = 3
DEFAULT_LIMIT = 20
LAMBDAS = (0.3, 0.5, 0.7, 1.0)
STRATEGY_NAMES = ["topk_raw", "greedy"] + [f"mmr_l{l}" for l in LAMBDAS]
# ---------------------------------------------------------------------------
# 真网候选池获取 + 离线缓存
# ---------------------------------------------------------------------------
from app.services.channel3.client import search_products_with_status  # noqa: E402


def _item_to_dict(item: Any) -> dict[str, Any]:
    """归一化商品 → json 安全 dict（缓存用）。

    `search_products_with_status` 已返回归一化 dict（`Channel3Result.items`），
    这里只需做 json 序列化适配；兼容对象形态便于复用。
    """
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    out: dict[str, Any] = {}
    for a in ("id", "name", "title", "category", "description",
              "price", "tags", "offer_url"):
        v = get(a, None)
        if v is not None:
            if a == "price":
                out[a] = float(v)
            elif a in ("tags",):
                out[a] = list(v) if isinstance(v, (list, tuple)) else [str(v)]
            else:
                out[a] = str(v)
    return out


def _items_dicts(items: list[Any]) -> list[dict[str, Any]]:
    return [_item_to_dict(it) for it in items]


async def fetch_pool(name: str, query: str, sem: asyncio.Semaphore,
                     limit: int, prefilter: bool) -> dict[str, Any]:
    """真网搜索一次，返回 {name, query, items(归一化 dict)}。"""
    cons = mk_constraints(query)
    sq, _dp, _a = build_search_query(query)
    async with sem:
        result = await search_products_with_status(sq, limit=limit)
    raw = list(result.items or [])
    kept = _prefilter_by_budget(raw, cons) if prefilter else raw
    return {"name": name, "query": query, "sq": sq,
            "status": result.status, "raw_n": len(raw),
            "items": _items_dicts(kept)}


async def fetch_all(limit: int, prefilter: bool) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(4)
    rows = await asyncio.gather(
        *[fetch_pool(n, q, sem, limit, prefilter) for n, q in SCENARIOS],
        return_exceptions=True,
    )
    return [r for r in rows if not isinstance(r, Exception)]


def save_cache(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cache(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def merge_rows(prior: list[dict[str, Any]],
               fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并两轮真网快照：后一轮成功行覆盖同名行（--update 多轮补全）。"""
    by_name: dict[str, dict[str, Any]] = {r["name"]: r for r in prior}
    for r in fresh:
        if r.get("items"):
            by_name[r["name"]] = r
    return [by_name[n] for n, _ in SCENARIOS if n in by_name]


# ---------------------------------------------------------------------------
# 相关度打分（rel）与重排策略
# ---------------------------------------------------------------------------
def rel_scores(query: str, items: list[dict[str, Any]]) -> list[float]:
    """rel = query token 在商品文本（name+category+tags）中的覆盖率。

    与推荐泳道同源（`_query_tokens`），换任何 query 都能算，不特调。
    """
    _q, sq, *_ = build_search_query(query)
    q_tokens = _query_tokens(sq)
    if not q_tokens:
        return [0.0] * len(items)
    scores: list[float] = []
    for it in items:
        text = " ".join([
            str(it.get("name") or ""),
            str(it.get("category") or ""),
            " ".join(str(t) for t in (it.get("tags") or [])),
        ])
        overlap = len(q_tokens & _query_tokens(text))
        scores.append(round(overlap / len(q_tokens), 6))
    return scores


def strategy_topk_raw(items: list[dict[str, Any]], rels: list[float],
                      k: int) -> list[dict[str, Any]]:
    """对照组：模拟 Channel3 返回顺序，无重排。"""
    return list(items[:k])


def strategy_greedy(items: list[dict[str, Any]], rels: list[float],
                    k: int) -> list[dict[str, Any]]:
    """实验组 1：纯相关度排序，无多样性抑制。"""
    ordered = sorted(zip(items, rels), key=lambda pr: pr[1], reverse=True)
    return [it for it, _ in ordered[:k]]


def strategy_mmr(items: list[dict[str, Any]], rels: list[float],
                 k: int, lam: float) -> list[dict[str, Any]]:
    """实验组 2：MMR 重排（生产 mmr_select + tiering.similarity）。"""
    return mmr_select(list(items), minmax(list(rels)), k=k, lam=lam,
                      min_rel=0.0, sim_fn=similarity)


def pick_topk(strategy: str, items: list[dict[str, Any]],
              rels: list[float], k: int) -> list[dict[str, Any]]:
    if strategy == "topk_raw":
        return strategy_topk_raw(items, rels, k)
    if strategy == "greedy":
        return strategy_greedy(items, rels, k)
    if strategy.startswith("mmr_l"):
        return strategy_mmr(items, rels, k, lam=float(strategy.split("_l")[1]))
    raise ValueError(f"unknown strategy: {strategy}")
# ---------------------------------------------------------------------------
# 指标：全部基于判定函数，零人工标注
# ---------------------------------------------------------------------------
def _verdict_of(cons: list[Any], item: dict[str, Any]) -> dict[str, Any]:
    """单件判定：evaluate(cons, [item]) —— 逐件有无 ❌（without 组合类约束）。"""
    r = evaluate(cons, [item])
    return {"failed": int(r.get("failed") or 0),
            "unknown": int(r.get("unknown") or 0),
            "total": int(r.get("total") or 0)}


def _group_judge(cons: list[Any], picked: list[dict[str, Any]]) -> dict[str, Any]:
    """列表级判定：top-k 这一组的可判定率（sat+unsat 占比）。"""
    r = evaluate(cons, picked)
    total = int(r.get("total") or 0)
    judgeable = int(r.get("passed") or 0) + int(r.get("failed") or 0)
    return {"judge_rate": round(judgeable / total, 4) if total else 0.0,
            "passed": int(r.get("passed") or 0),
            "failed": int(r.get("failed") or 0),
            "unknown": int(r.get("unknown") or 0)}


def _stats_of(picked: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [float(it.get("price") or 0) for it in picked if it.get("price")]
    mean = sum(prices) / len(prices) if prices else 0.0
    cv = 0.0
    if len(prices) > 1 and mean > 0:
        variance = sum((x - mean) ** 2 for x in prices) / (len(prices) - 1)
        cv = round(math.sqrt(variance) / mean, 4)
    categories = {str(it.get("category") or "") for it in picked}
    return {"diversity": len(categories), "price_cv": cv}


def score_strategy(query: str, cons: list[Any], items: list[dict[str, Any]],
                   rels: list[float], strat: str, k: int) -> dict[str, Any]:
    picked = pick_topk(strat, items, rels, k)
    per_item = [_verdict_of(cons, it) for it in picked]
    hit = any(v["failed"] == 0 for v in per_item)
    # MRR：第一个无 ❌ 的商品（按策略排序后）位置倒数；池内可能不足 k 件。
    rank = next((i + 1 for i, v in enumerate(per_item) if v["failed"] == 0), None)
    group = _group_judge(cons, picked)
    stat = _stats_of(picked)
    return {
        "hit": bool(hit),
        "mrr": round(1.0 / rank, 4) if rank else 0.0,
        "judge_rate": group["judge_rate"],
        "diversity": stat["diversity"],
        "price_cv": stat["price_cv"],
        "rank": rank,
    }


def run_ablation(rows: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """rows 来自 fetch_all / load_cache。返回每 query 的策略对比。"""
    result: list[dict[str, Any]] = []
    for row in rows:
        items = [dict(it) for it in row.get("items") or [] if isinstance(it, dict)]
        if not items:
            continue
        cons = mk_constraints(row["query"])
        rels = rel_scores(row["query"], items)
        per: dict[str, Any] = {"name": row["name"], "query": row["query"],
                               "n_items": len(items), "strategies": {}}
        for strat in STRATEGY_NAMES:
            per["strategies"][strat] = score_strategy(
                row["query"], cons, items, rels, strat, k)
        result.append(per)
    return result


def _mean(rows: list[dict[str, Any]], strat: str, field: str) -> float:
    vals = [r["strategies"][strat][field] for r in rows]
    return round(sum(vals) / len(vals), 4) if vals else 0.0
def render(rows: list[dict[str, Any]], k: int) -> str:
    buf = io.StringIO()
    buf.write(f"检索/重排策略消融（k={k}，真网返回 + 判定函数当裁判，{len(rows)} 个 query）\n")
    buf.write("\n" + "=" * 100 + "\n")
    for r in rows:
        buf.write(f"\n[{r['name']}] 候选 {r.get('n_items')} 件\n")
        buf.write(f"{'策略':<12}{'Hit':>5}{'MRR':>7}{'可判定率':>9}{'多样性':>7}{'价格CV':>8}\n")
        for strat in STRATEGY_NAMES:
            s = r["strategies"][strat]
            buf.write(f"{strat:<12}{str(s['hit']):>5}{s['mrr']:>7}"
                      f"{s['judge_rate']:>9}{s['diversity']:>7}{s['price_cv']:>8}\n")
    buf.write("\n" + "=" * 100)
    buf.write(f"\n汇总（均值，k={k}）：\n")
    buf.write(f"{'策略':<12}{'Hit率':>7}{'MRR':>7}{'可判定率':>9}{'多样性':>7}{'价格CV':>8}\n")
    for strat in STRATEGY_NAMES:
        buf.write(f"{strat:<12}{_mean(rows, strat, 'hit'):>7}"
                  f"{_mean(rows, strat, 'mrr'):>7}"
                  f"{_mean(rows, strat, 'judge_rate'):>9}"
                  f"{_mean(rows, strat, 'diversity'):>7}"
                  f"{_mean(rows, strat, 'price_cv'):>8}\n")

    buf.write("\n读法：topk_raw=模拟 Channel3 原序；greedy=只按相关度排序；")
    buf.write("mmr_l0.3/l0.5/l0.7/l1.0=MMR 重排 λ 扫描。")
    buf.write("Hit/MRR/可判定率全部来自生产判定函数 evaluate（零人工标注）；")
    buf.write("多样性=top-k 唯一品类数，价格CV=价格离散度（MMR 的本职是抑制扎堆）。\n")
    buf.write("注意：MRR 的 rank 是「第一个无 ❌ 商品」的位置——组合类约束（数量/总预算）\n")
    buf.write("对单件天然 unknown，这一行如实反映判定层的可判定边界，不是缺陷。\n")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=DEFAULT_K, help=f"top-k（默认 {DEFAULT_K}，与方案卡数一致）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"真网搜索 limit（默认 {DEFAULT_LIMIT}）")
    ap.add_argument("--cache", metavar="PATH", default="docs/retrieval_live.json",
                    help="真网结果缓存路径（默认 docs/retrieval_live.json）")
    ap.add_argument("--offline", action="store_true", help="重放缓存，不触网（需 --cache 存在）")
    ap.add_argument("--json", metavar="PATH", help="把结构化结果写到该文件")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="关掉预算 prefilter（默认模拟真实流水线的预算剔除）")
    ap.add_argument("--update", action="store_true",
                    help="合并到已有缓存：后一轮成功行覆盖同名行（多轮补全）")
    args = ap.parse_args()

    cache = Path(args.cache)
    if args.offline:
        if not cache.exists():
            print(f"[FAIL] --offline 需要缓存文件 {cache}（先用真网跑一次生成）")
            return 1
        rows = load_cache(cache)
        print(f"[offline] 重放 {cache}（{len(rows)} 个真网返回快照）")
    else:
        prior: list[dict[str, Any]] = []
        if args.update and cache.exists():
            prior = load_cache(cache)
            print(f"[update] 已载入 {len(prior)} 个旧快照，只合并成功行")
        print(f"[live] 真网搜索 {len(SCENARIOS)} 个 query …（需网络，约 1-2 分钟）")
        fresh = asyncio.run(fetch_all(limit=args.limit, prefilter=not args.no_prefilter))
        rows = merge_rows(prior, fresh) if prior else fresh
        save_cache(rows, cache)
        n_ok = sum(1 for r in rows if r.get("items"))
        print(f"[live] 缓存已写入 {cache}：{n_ok}/{len(rows)} 个 query 有候选")

    result = run_ablation(rows, k=max(1, int(args.k)))
    print(render(result, k=max(1, int(args.k))))

    if args.json:
        out = Path(args.json)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结构化结果已写入 {out}")

    # 观察性指标,无硬阈值——真网数据呈现事实,λ 曲线重合本身就是结论。
    print("\n说明:本脚本是观察工具（呈现事实），不做阈值断言——真网数据不可控，"
          "λ 曲线重合或分化都是可讲的结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())