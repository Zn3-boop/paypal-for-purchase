# Business Decomposition（业务拆解一页纸）

> 目的：把「自然语言采购需求 → 可信成交」这条链路拆成 9 段，每段讲清
> **输入 → 处理 → 输出 → 失败路径 → 为什么这样设计**。面试官问业务逻辑时，
> 对着这一页讲，不用翻 600 行 README。全部内容都可从代码/数据复算。

## 链路全景

```
用户一句话意图
  │ ① 意图解析 payment_parse_intent
  ▼
结构化意图（买什么 / 买多少 / 预算 / 谁出钱）
  │ ② Channel3 检索（真实商品） + ③ 本地 catalog_k1 兜底
  ▼
候选商品池（三态：ok / empty / error）
  │ ④ 约束抽取 _simple_rule_extraction（功能/材质/颜色/数字/禁忌）
  ▼
规则约束集
  │ ⑤ 满足度判定 satisfaction.evaluate（✅ / ❔ / ❌ 三态，带 rule+证据）
  ▼
逐条判定（组合层 + 逐件层）
  │ ⑥ 预算三重校验 + ⑦ 双方案生成（A 低价 / B 耐用，MMR 重排 + 原因码）
  ▼
方案 A/B + 每件可复算的「推荐依据」
  │ ⑧ 人在回路确认（HITL gate：批准 / 修改 / 拒绝，预算守卫）
  ▼
用户确认 → PayPal 沙盒创建订单 → 用户去 PayPal 页确认 → 捕获
  │ ⑨ 金额守卫 + 分摊 + 审计事件 + JSONL/PG 双写
  ▼
订单落档、审计链可追溯
```

## 逐段拆解

| 段 | 输入 → 处理 → 输出 | 失败路径（诚实降级，绝不假装） |
|---|---|---|
| ① 意图解析 | 一句话 → 规则/LLM → 结构化意图（数量/预算/品类） | 抽不出数量 → quantity=1，不明说；抽不出预算 → 无预算时不伪造 600 |
| ②③ 检索 | 意图 → Channel3 query 构造（中英映射 + 规格注入）→ 候选池 | 检索错误 → `error` 三态 + 本地目录降级 + 文案「暂时不可用」；0 命中 → `empty`，不硬凑 |
| ④ 约束抽取 | 意图 → 规则表 → 约束集（scope: total/per_item） | 抽不出 → 该条约束不进判定，不凭空造约束 |
| ⑤ 判定 | 约束+商品 → 三态（sat/unsat/unknown）+ rule + 证据 | 无证据 → ❔（unknown），绝不假装 ✅；商品自述亮面 vs 哑光 → 确证 ❌ |
| ⑥ 预算 | 组合总价 / 单件价 vs 预算上限 → 三重强制校验 | 超预算 → 拦截 + 审计；价差波动只提示（P1-1 才做支付前重校验） |
| ⑦ 方案 | 候选 → A/B 双方案 + MMR 重排 + 原因码 | 无可行方案 → `fallback`（不编造第二个方案） |
| ⑧ HITL | 方案 → 用户批准/修改/拒绝 → 才创建订单 | 拒绝/修改 → 重新生成；金额被改 → `payment_amount_mismatch` 守卫 |
| ⑨ 落档 | 订单+分摊+审计 → JSONL 追加 + PG 双写 | PG 不可用 → 纯 JSONL（CI/离线默认）；幂等去重 `insert_audit_if_absent` |

## 量化指标（全部可复算）

| 指标 | 数值 | 怎么复算 |
|---|---|---|
| 自动化测试 | **436 个**（基线 396 → +40） | `cd apps/server && python -m pytest -q` |
| 质量门禁 | 20 句 × 真实检索，sat=91 / unsat=0 / unknown=12 | `python scripts/quality_gate.py`（需网络） |
| 判定诚实性 golden | **7/7 通过**（6 现场 Bad Case → 0 残留） | `python scripts/satisfaction_golden.py`（离线） |
| 检索韧性 | 硬超时 20s / 熔断 60s / 缓存 30min | 代码常量 + 韧性测试 |
| 降级路径 | 3 态（ok/empty/error）+ 本地目录兜底 | `test_channel3_client_resilience.py` |
| 审计守卫 | 金额不一致 → 拒绝标记 paid | `payment_amount_mismatch` 事件 |
| 持久化 | JSONL 追加 + PG 双写幂等 | `test_pg_store.py` / `test_restart_persistence.py` |
| 单元级纯函数 | satisfaction 判定为纯函数（无副作用） | 模块 docstring 声明 |

## 为什么这些设计对（业务逻辑自洽的四个锚点）

1. **诚实分级先于功能**：三态判定（✅/❔/❌）+「缺证据 ≠ 违反」写进模块红线（`satisfaction.py`），
   「没查到的」绝不说成「查不到的」——这是整个 demo 的立身之本，不是后加的补丁。
2. **失败是可解释的路径，不是黑洞**：三态检索、熔断、降级、本地目录兜底，每一条都落到
   UI 文案 + 审计事件。评委看到「暂时不可用」能立刻明白发生了什么。
3. **人在回路是安全边界**：AI 只做检索/比价/预算校验/方案生成，支付必须由用户在 PayPal
   页面确认；下单金额 ≠ 捕获金额 → 拒绝落 paid。这是「采购代理」与「自动花钱机器人」的分界线。
4. **每个数字都有出处**：436 测试、7/7 golden、sat=91 都是脚本跑出来的，不是 PPT 数字。
## 方法论：评测集 + 对照实验 + Bad Case 迭代（三层防线）

对标成熟 RAG/检索项目的做法（评测集 → 对照实验 → Bad Case 定位 → 迭代量化），
本项目把「判定诚实性」做成同样的三层，全部在仓库里可复算：

1. **单元级 failing-first**：每一个现场 Bad Case 先写失败用例
   （`tests/test_channel3_field_extraction.py`），再修 `satisfaction.py`，测试变绿后永远留在回归集。
2. **golden set 离线自检**：`scripts/satisfaction_golden.py` 把 6 个现场 Bad Case 的
   「修复前表现 → 根因 → 现在实判（含命中的 rule）」固化成 7 个场景，3 秒离线跑完，
   断网也能给评委当场演示「我们复盘过现场失败」。
3. **真网质量门禁**：`scripts/quality_gate.py` 20 个模板句走真实抽取→检索→判定，
   带 `--max-unknown/--min-sat` 断言可挂 CI。

三层对应：单测保证「改坏了立刻红」、golden 保证「诚实性叙事可当场复算」、
gate 保证「真实链路的全局不回归」。

## 刻意不做（写在边界里，避免过度设计）

- 不引入向量库/FAISS、不引入多 Agent 编排：当前检索质量问题用「映射表 + 结构化证据 +
  判定三态」已解决，上这些属于过度设计（YAGNI）。
- AI 不自动发起支付：人在回路是硬边界，付款必须用户在 PayPal 页确认。
- 不关 SSL 校验：降级只换信任来源（系统库→certifi），绝不 `verify=False`。
- 佣金不参与选品排序：采购者的钱不该被平台收入导向（字段保留但排序不用）。
- 单租户、无登录：检索记忆是团队级的（demo 边界，README 明说）。