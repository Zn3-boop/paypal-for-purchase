# Agentic Payment - Authorization-Boundary Demo (PayPal Hackathon)

> **The agent can spend your money - but only inside the authorization boundary you control.**

A LangGraph agent that buys things for you and *explains every decision*: it routes purchase
intent into a 7-node payment subgraph, checks your trust profile + savings-goal constraints,
stops for your approval when needed, creates a real **PayPal sandbox order**, captures it,
then writes A/B-graded evidence back into your trust score.

Zero credentials required to run: `PAYMENT_MODE=mock` runs the full pipeline (approval ->
create -> capture) against a mock PayPal client. Flip to `real` with sandbox credentials and
the same flow hits `api-m.sandbox.paypal.com`.

## Judge path (about 3 minutes, all in the web UI)

The app has three panels: **选购方案 / Plans**, **审计时间轴 / Audit**, **订单档案 / Orders**.

1. In the request box type **your own** sentence — nothing is preset: no budget, no constraint, no
   plan exists until you submit. Example shape:
   **「社团采购：预算不超过 <你填的金额> 美元，不要 <你不想要的材质>，给户外活动用」**
   -> only that sentence's budget/taboo are extracted into the dashboard, and two plans (**A**
   low-price / **B** best-match) appear below with real trade-off numbers.
2. Check the evidence line under the plans: the query actually sent to **Channel3**, how many
   candidates came back, and which source was used (`channel3` or the local fallback catalog).
3. Click **选这个方案** -> the confirmation modal opens with the amount, a natural-language cart
   edit box, the budget guard result, and (in group mode) the split-bill preview.
4. Type **「删掉最贵的那件」** -> the cart total really drops and the modal re-checks budget.
   Confirm -> PayPal **sandbox** approval page -> approve.
5. You land back on the app: the paid order auto-expands in **订单档案** with its state-machine
   transitions, and **审计时间轴** shows the whole chain
   (`constraint_changed` -> `plan_generated` -> `cart_modified` -> `review_confirmed` ->
   `confirmed` -> `paying` -> `payment_completed` -> `paid`).
6. Ask **「我的订单到哪一步了？」** -> the answer comes from the state machine, not from the LLM.

Restarting the API does **not** lose your work: live sessions (constraints, cart, plan snapshot),
the order archive, the audit timeline and the 🧠 检索记忆 row are all on disk and reloaded on
startup (`tests/test_session_persistence.py`, `tests/test_restart_persistence.py`,
`tests/test_search_memory.py`). The only in-memory piece is the pending PayPal approval-URL map;
if it is lost, the callback recovers the order from the `session_id` in the return URL.

The single-purchase authorization-boundary subgraph described below is still fully wired at the
API level (`POST /api/v1/payment/agent` -> `approve` -> `callback`, covered by the payment-agent
integration tests). The web UI deliberately exposes only one checkout path per plan so the same
purchase cannot be re-composed behind the user's back.

## Why an authorization boundary?

LLMs are great at *deciding*; they are dangerous at *spending*. This demo answers:

- **How much can the agent spend without asking?** -> `ProgressiveAuthzEngine` (trust score +
  auto-threshold; new users require approval on every purchase, thresholds only shrink).
- **What must always ask?** -> user-set savings goals (`GoalMemory`), category-conflict and
  monthly-cap rules, risk checks.
- **How does the user regain control?** -> HITL approval gate (`approve` / `reject` / `modify`),
  and the agent must explain afterwards with graded evidence.

## Group Purchase mode: the boundary is even stricter

The single-purchase flow above is the same authorization boundary applied to one purchase. The app
also ships a **group purchase** flow (ECV 社团采购) where the same principle scales up: **the human
holds final transaction authority at every stage**, and the agent's hooks are mechanically
prevented from crossing it.

- **约束优先，改动能解释** — hard constraints (budget caps, taboos) pre-filter the catalog
  before any strategy runs. Natural-language additions still need the LLM to extract a
  structured constraint, but **delete intents never reach the LLM**: the front end matches
  your words against the existing constraints and only asks the server to `DELETE` a specific
  one (0-hit / multi-hit / add+delete mixes are surfaced, never silently guessed).
- **AI 无支付发起权** — `create_order` is the *only* payment trigger and it must pass the
  HITL review confirmation first: `draft → confirmed` is guarded by an audit record of your
  review, `confirmed → paying` is guarded by a budget PASS, and the amount is **never taken
  from the front-end body** - it is re-derived from the session cart server-side. Repeated
  clicks while paying are idempotent: the same PayPal approval URL is returned, no second
  order is created.
- **实扣对账副门** — `paying → paid` only happens after the PayPal capture amount matches
  the recorded order amount exactly (to the cent). A mismatch is *not* silently forgiven: it
  writes a `payment_amount_mismatch` audit event and cancels the order.
- **订单查询只读** — natural-language order queries are read-only and session-ownership
  enforced by the server: a foreign `ord_xxx` in your question is answered“not found”, never
  leaked; queries never touch the state machine.
- **审计留痕不是装饰** — every decision above lands in an append-only JSONL audit timeline:
  constraint changes, plan generation, cart edits, review confirmation, each state-machine
  transition, payment completion and guardrail failures - the story of *why* money moved is
  inspectable end to end.

## How plans A and B are compared (and why a judge can check it)

Both plans are assembled from the **same candidate pool** (hard constraints are applied first, then
A and B run on that identical pool with the same budget cap and the same min/max item count). They
differ only in their objective function:

| Plan | Objective function | What it is *not* |
|---|---|---|
| **A — low price** | minimize total price under the budget cap | not "worse quality"; price is not a quality proxy |
| **B — best match** | maximize the sum of Channel3 keyword-match scores (`match_score`) under the same cap | not "higher rated"; there are no ratings or reviews in the data |

`GET /api/v1/plans` returns this contract in `comparison.criteria`, the raw numbers in
`comparison.metrics` (`total_a/b`, `relevance_a/b`, item and shared counts), and a Pareto verdict in
`comparison.pareto`:

- `trade_off` — A is cheaper **and** B has the higher match score: both sit on the Pareto frontier,
  so it is a genuine preference call, not "one plan is worse".
- `a_dominates` / `b_dominates` — one plan is no worse on **both** axes. The UI says so explicitly
  instead of dressing a dominated plan up as a trade-off.

The UI prints the same three lines under **⚖️ 差异对比**, so the numbers on screen are the numbers
in the API response. Regression tests: `tests/test_comparison_criteria.py`.

Why this framing is defensible rather than invented: it is the standard constrained
dual-objective comparison used by comparison-shopping surfaces (price axis vs relevance axis, with
hard filters applied first), and the Pareto check is textbook multi-objective optimization. We do
not claim B is "more durable" — nothing in the data supports durability, so the UI never says it.

## Channel3 retrieval memory (search history, not profiling)

Every submitted request is appended to `data/search_memory/searches.jsonl`, and every plan selection
to `data/search_memory/selections.jsonl` (`SEARCH_MEMORY_DIR` overrides the location). The
🧠 **检索记忆** row under the request box lists deduplicated recent queries (with how many times each
was searched) plus the last selected plan (`方案 B · $393.00 · 8 件`). Clicking an entry re-submits
that exact sentence — nothing else.

Honest boundary: the history is **never** merged into the current Channel3 query. That would be
fake personalization (last week's taste silently contaminating this week's request). The memory is a
shortcut to re-run a search, and a visible record that Channel3 is being used across sessions
(`GET /api/v1/search-memory`, `tests/test_search_memory.py`).

### The third lane: Channel3 recommendations with a recomputable source

The 🧠 row only re-runs an old sentence. The ✨ **第三条泳道 · Channel3 推荐** strip under the A/B
diff actually **searches again**: it takes the recent queries plus the items of the last selected
plan as anchors, sends **one** real Channel3 query, filters it with the same relevance gate the
planner uses, drops anything already in the cart or already picked, then re-ranks the survivors with
**MMR** (λ·rel − (1−λ)·sim-to-A∪B, λ=0.6) and shows at most three cards. Sorting and per-card
reason come from pure functions with a judgment line each (see 规格 B/C below), not from a model.

Each card carries a source a judge can verify instead of a label:

```
你搜过「社团采购 户外 装饰」2 次 → 本次检索「sports equipment | 社团采购 户外 装饰」→ 命中 14 件，展示 3 件
```

`query_used` is the literal string sent to Channel3 and `search_hits` is how many products came
back — both in `GET /api/v1/recommendations`. Paste that query into Channel3 and you should see
similar candidates; if you cannot, the card's "because" is decoration, not evidence.

The recommendations are deliberately **not** folded into plans A/B: the comparison has to be
reproducible from the request alone (same candidate pool, same two objectives). If yesterday's
searches changed the pool, the same sentence would yield different plans and the `criteria` block
would stop being checkable. So history is quarantined in this one lane — the UI says so, and every
card is tagged `已在方案 A` / `已在方案 B` / `两方案都没有`, which is exactly how the lane complements
the comparison rather than contaminating it. A `两方案都没有` tag never stands alone: it carries the
rule that excluded it (`同品类已有更便宜` / `加它会超预算 $X` / `本次需求没要求这个品类`), so the
third card is a considered-and-rejected option, not filler. Channel3 responses are cached for 30
minutes, so a slow first search cannot re-trigger a timeout mid-demo.

Adding a card to the cart uses the same guardrail as cart editing: over a hard budget it is
**blocked with the cart untouched** (`guardrail.status="blocked"`; `force=true` to override), and it
writes the existing `cart_modified` audit event with
`modification_type="add_from_recommendation"` — no new event type, so the audit event table and
`golden_path_verify.py` stay valid. If Channel3 is unavailable you get an empty list plus a
`disabled`/`error` status, never local-catalog items dressed up as recommendations
(`tests/test_recommendations.py`).

### 演示机 DNS 污染：应用层 pin，而不是 hosts / 换网络 / replay

2026-11-10 实测：演示机（受限网络）的网关 DNS 把 `api.trychannel3.com` 的 CNAME 目标
（`d-aklj24vug8.execute-api.us-east-1.amazonaws.com`）解析成 `127.0.0.1` 且时好时坏 ——
所以症状是「DNS 解析失败」与「连接失败」同时出现。诊断判据：`nslookup <域名> 8.8.8.8`
能拿到真实 AWS IP，且 `curl --resolve api.trychannel3.com:443:<IP> https://api.trychannel3.com/`
返回 200 → 服务端正常、出网正常（PayPal 沙盒 401 为对照组），**只是本机 DNS 被污染**。

解法刻意选**应用层 DNS pin** 而不是改 hosts 或做 replay：

- 无需管理员权限（录制机实测 `IS_ADMIN=False`，hosts 写不了）；
- 不关 TLS 校验：URL 仍是 `api.trychannel3.com`，SNI / Host / 证书校验全部照旧，
  只是 connect 阶段的目标 IP 固定为 `CHANNEL3_DNS_PIN` 里的真实地址；
- 影响面最小：只 pin `api.trychannel3.com` 与其 CNAME 目标两个名字，PayPal/LLM 等不受影响；
- 是「故障定位后的工程修复」，不是「用快照掩盖故障」—— 事件表与审计语义不变。

用法：`apps/server/.env` 设置 `CHANNEL3_DNS_PIN=3.212.207.12,...`（逗号分隔）；换到正常网络清空即可。
回归：`tests/test_channel3_client_resilience.py::test_dns_pin_pins_api_host_and_ignores_non_pin_hosts`。

### 规格 B/C：MMR 重排 + 每张卡一个可复算原因码

泳道的**排序**和**理由**都不是模型黑盒 —— 判定式全是纯函数，README 各一行：

```
rel(c)   = min(1, |tokens(c) ∩ 本次检索锚点| / 4)      # 绝对相关度 [0,1]，先过 floor
floor:   rel(c) < 0.25 → 不进入候选
score(c) = λ·rel(c) − (1−λ)·max_{s∈S} sim(c,s)          # λ=0.6，贪心逐张取
sim(c,s) = 1 − tier(c,s) / 6                            # S = 方案 A∪B ∪ 已选推荐卡
```

- `rel` 的锚点只来自**本次检索**（意图句 + 最近一条本会话搜索），不含已选商品名 ——
  选品是方案侧信号，混进来会让「复读方案」看起来像「贴近历史」（会话隔离回归：
  `tests/test_recommendations.py::test_other_sessions_do_not_poison_my_recommendation_query`）。
- floor 施加在**归一化之前**的绝对相关度上（`minmax` 只在打分尺度上做）：两个候选时
  最小值会被归一化成 0，在归一化之后 floor 会把第二名误杀。
- `sim` 来自替代分级而不是文本相似度：cross-genre 的两张 vinyl 标题都含
  "vinyl/record"，文本相似度把它们罚成「重复」，tier 不会。

每张卡带 `reason_code`（主码）+ `reason_code_secondary`（至多一个辅码）+
`reason_code_text`。判定按固定优先级取第一个命中，源码在
`apps/server/app/services/group_purchase/reason_codes.py`：

| code | 判定式（一行） |
|---|---|
| `FILLS_CATEGORY_GAP` | rel ≥ 0.25 且 item 品类 ∉ A∪B 的品类集合 |
| `CHEAPER_ALTERNATIVE` | 与方案里某件 tier ≤ T4 且价格更低（省最多那件） |
| `SAME_RELEASE_OTHER_EDITION` | 与方案里某件 tier == T1（同款不同规格，未更便宜） |
| `SAME_ARTIST` | 与方案里某件 tier ∈ {T2, T3}（同品牌/同艺人，同类型） |
| `SAME_GENRE` | 与方案里某件 tier ∈ {T4, T5}（同类型，不同品牌） |
| `HISTORY_DIRECTION` | 以上都不中但 rel ≥ 0.25（同方向，不是同款） |
| `""` | rel < 0.25 或无方案可依 → 不给码（这张卡本就不该出现） |

**不伪造条款**：解析不出品牌/类型（tier 落到 T6）时绝不产出 `SAME_ARTIST` /
`SAME_GENRE`，直接降到 `HISTORY_DIRECTION` 或空码；tier=0（同一件）且更便宜时主码必须
是 `CHEAPER_ALTERNATIVE` ——「同一件」不是「另一个版本」。`relevance` 字段就是上面的
rel；`HISTORY_DIRECTION` / `SAME_GENRE` 会是大多数卡的主码（推荐的本质是「同方向」），
UI 因此从不暗示「这张更值得买」。回归：`tests/test_mmr.py`、`tests/test_reason_codes.py`。

`scripts/preflight_demo.py` 把这些当成开录硬门：3 张卡都带 `reason_code`、每卡 rel ≥ 0.25、
`relevance_reselect` 模式下 `upgrades` 必须为空、`substitution` 模式下每项替换 tier ≤ T3。

### 订单超时：为什么是「惰性判定」而不是 Redis TTL

`paying` 订单带 `expires_at`（进入 paying 时写入，默认 30 分钟，`ORDER_PAYING_TTL_SECONDS` 可覆盖）。
读接口（`GET /orders`、`GET /orders/{id}`、`GET /orders/{id}/status`，以及 PayPal 回跳兜底）每次访问时
顺手判断：**paying 且已越过 `expires_at` → `paying → expired`，并写一条 `order_status_changed`
审计事件**（`actor=lazy_expiry`，reason 带 `expires_at` 与 TTL）。没有定时器，没有后台循环。

| 方案 | 为什么不用 |
|---|---|
| Redis TTL + keyspace notification | 要引入 Redis 依赖；且通知是 fire-and-forget —— 订阅者掉线期间的过期事件永久丢失，还得多维护一个消费者。比「读的时候顺手判一次」多出连接、订阅、重连、丢事件四类故障点。 |
| ZSet 延时队列 / 定时扫描 | 需要常驻调度器 + 分布式锁 + 重复消费处理；本项目的订单量下收益为 0，新增的故障点却是一整类。 |
| RabbitMQ 死信队列（DLX） | 为一个 30 分钟超时引入消息中间件，是用生产级基础设施解决演示级问题。 |
| 时间轮 | 内存态定时器，重启即丢；与「订单档案已落盘」的事实不一致。 |

**代价（一行）**：没人访问的过期订单不会立刻过期 —— 它在下一次读（列表 / 详情 / 轮询 / 回跳）时才迁移。
系统不占库存、不锁资金，所以这个延迟只影响展示，不影响正确性。

**收益**：过期是一个**可复核的状态转移**（有审计事件、有 reason、前端可见），而不是「一个 Redis key
悄悄消失」。评委可以沿着审计时间轴逐条回放「这单什么时候、为什么变成过期」。

**外部事实优先**：`expired` 不是终局的本地断言，只是时间推断。若 PayPal 回跳 `capture=COMPLETED`，
订单走 `expired → paid`（状态机里 `expired → {paid}` 是唯一允许的「倒流」入口），审计 reason 明确写
「本地判定已过期，但 PayPal capture 已完成（金额 X），以支付事实为准」，并落机器可读原因码
`reason_code="remote_completed_after_local_expiry"` —— Q&A 可直接 grep 审计流水复算
「本地推断被外部事实推翻」这个时刻。D2 金额副门仍然生效 —— 金额不一致绝不落 `paid`。同理，启动对账
（`reconcile_sessions_on_startup`）优先采信审计里的 `payment_completed`，不会被惰性过期抢先标记。
回归：`tests/test_order_expiry.py`。

## What is deliberately demo-grade (honest limitations)

- **No user login.** Sessions are keyed by `session_id` only, and are persisted as one JSON per
  session under `data/sessions/` (`SESSION_STORE_DIR` overrides it): constraints, plans, cart and
  order id survive a restart. Orders and audit events are append-only JSONL; search memory is
  JSONL too. The **only** in-memory piece is the pending PayPal approval-URL map — the callback
  falls back to the `session_id` in the return URL when that map is lost.
- **Retrieval memory is team-wide**, not per-user (single-tenant demo).
- **`match_score` is lexical** (title/tag overlap with the request), not a semantic or quality
  score. The UI says this in the comparison-criteria block.
- **PayPal is sandbox only.** Real capture is mocked in `PAYMENT_MODE=mock`.

## Architecture

```
Web Frontend (React + Vite) -- Plans / Audit / Orders panels -- /plans/intent | /cart | /orders
        |
        v
FastAPI (apps/server) -- build_agent_graph() -- LangGraph (compiled, singleton + MemorySaver)
        |
        |-- payment_parse_intent     intent/amount/category extraction
        |-- payment_taste_select     catalog match + enjoyment classification
        |-- payment_authority_check  progressive authz engine + goal-conflict detection
        |-- payment_approval_gate    HITL interrupt gate (approve/reject/modify)
        |-- payment_paypal_create    create PayPal Sandbox Order (mock | real)
        |-- payment_paypal_capture   capture order
        +-- payment_explain          LLM post-hoc explanation + A/B evidence write-back
        |
        +-- TrustProfile (in-session) + memory_store (cross-session persistence)
```

The payment subgraph is wired into the same compiled graph that serves the rest of the
agent (routing detects purchase intent before cultural-recommendation paths), so the
authorization boundary is *not* a separate service - it is a first-class part of the
agent loop.

## Data Source
This prototype supports two interchangeable data sources for demo:
1. Channel3 API (official hackathon sponsor) - real product search, normalized into the local item shape.
2. Local mock dataset (`catalog_k1`), used as an honest fallback when Channel3 is unavailable or returns nothing relevant.

Channel3 results are filtered for relevance to the request, same-name + same-price duplicates are
dropped, and items that came back but did not match are shown *separately* as "closest results"
(`sponsor_related`) - they never enter a plan as if they were matches.

## Quick start

Requirements: **Python 3.12+**, **Node.js 20+** (npm is enough).

```bash
# 0. Clone
git clone https://github.com/Zn3-boop/paypal-for-purchase.git
cd paypal-for-purchase

# 1. Backend
cd apps/server
python -m venv .venv && .venv\Scripts\activate        # Windows: .venv\Scripts\activate | macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                                # Windows: copy | macOS/Linux: cp
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. Frontend (second terminal, from the repo root)
cd apps/web
npm install
npm run dev                                           # http://localhost:3000, proxies /api -> :8000
```

**It runs out of the box with zero external keys** - `PAYMENT_MODE=mock`, `LLM_MODE=mock`
are the safe defaults in `.env.example`, so judges can clone-and-run without credentials.
Set `FRONTEND_BASE_URL=http://localhost:3000` in `apps/server/.env` so PayPal return/cancel
redirects land back in the app after approval.

**One-command Docker alternative** (no local Node/Python needed):

```bash
# repo root — keys are optional; the mock defaults run without them
docker compose up --build
```

API on http://localhost:8000, app on http://localhost:3000. Server state (`sessions/`, `orders/`,
`audit_logs/`, `search_memory/`) is written to a Docker named volume, so `docker compose restart`
demonstrates real restart-proof persistence rather than a page reload. Before filming a walkthrough,
`python scripts/preflight_demo.py --session-id <id>` must exit 0 (gates: A/B differ with non-empty
`comparison.upgrades`, and the recommendation lane is `ok` with 3 cards).

## Environment variables (apps/server/.env)

| Variable | Default | Purpose |
|---|---|---|
| `PAYMENT_MODE` | `mock` | `mock` runs the whole flow offline; `real` hits PayPal sandbox API |
| `LLM_MODE` | `mock` | `mock` uses rule-based intent/explanation; `real` calls the OpenAI-compatible LLM |
| `LLM_BASE_URL` | `https://maas.qianwenaiapi.com/compatible-mode/v1` | qwen platform (DashScope) OpenAI-compatible endpoint; any official OpenAI-compatible endpoint works (e.g. Zhipu open.bigmodel.cn + GLM-5.3-Flash) |
| `LLM_MODEL` | `qwen3.8-flash` | model used when `LLM_MODE=real` |
| `LLM_API_KEY` | - | required only for `LLM_MODE=real` |
| `PAYPAL_MODE` | `sandbox` | `sandbox` for demo |
| `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET` | - | merchant app credentials from the PayPal Developer Dashboard (Business account); required only for `PAYMENT_MODE=real` |
| `PAYPAL_SANDBOX_API` | `https://api-m.sandbox.paypal.com` | sandbox API base |
| `CHANNEL3_API_KEY` | - | Channel3 real product search; empty → honest fallback to local `catalog_k1` |
| `CHANNEL3_DNS_PIN` | (empty) | 演示机 DNS 被污染时的应急：逗号分隔的真实 IP 列表，应用层固定 connect 目标（TLS/SNI/Host 仍按域名校验，不关校验、无需管理员权限）；正常网络留空 |

## Real Mode (for judges)

若需演示真实链路，按以下三步切换（`.env` 不提交到 git）：

1. `PAYMENT_MODE=real` — 走 PayPal 沙盒 API（需填 `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET`）。
2. `LLM_MODE=real` — 走真实 LLM（需填 `LLM_API_KEY`，端点 `LLM_BASE_URL`）。
3. `CHANNEL3_API_KEY=<你的 key>` — 走 Channel3 真实商品搜索；为空则自动降级到本地 K1 catalog。

启动时服务端会打印一行模式声明，例如：
`[mode] llm=real | payment=real | paypal=sandbox | channel3=real`
评委可据此确认演示时处于预期的真实/降级路径。

## Demo script (话术)

| Step | Input | What happens |
|---|---|---|
| 1 | 🎯 设置目标：**我要存钱买相机** | `POST /payment/goal` stores a structured `SavingGoal` |
| 2 | **帮我买一张周五晚上听的爵士唱片** | enjoyment spend (music >= $20) conflicts with the savings goal -> authority verdict `need_approval` (blocked rule) |
| 3 | 点 **✓ 批准并支付** | `POST /payment/approve` resumes the interrupted gate, creates + captures order |
| 4 | 再问 买一杯咖啡 或重复批准 | trust score rises; after enough A-grade approvals small low-risk spends may auto-allow (`allow_auto`) |
| 5 | 点 **✏️ 修改后放行** 试试 | `modify` decision is treated as A-grade evidence (用户修改金额后批准支付) and still captures |


## Technical notes for judges (three design decisions)

Three things reviewers often ask about - here is the honest short version:

1. **Strict response serialization.** We split the LLM SDK response before serializing:
   the typed `parsed` field is exported on its own and the raw response is kept intact for
   tracing. No decision data is lost, and the agent state flow is unchanged. The UI only
   shows what the model actually produced.
2. **Evidence has a lifetime, on purpose.** Per-round decision evidence (item choice,
   reasoning, score breakdown, and local match tags) lives in the session state and disappears
   with the session - clean for a single walkthrough. The only cross-session persistence is
   the **trust score**, written through an A/B-graded evidence gate (owner-bound, low-grade
   inferences are never allowed to silently change it). We deliberately avoid dumping every
   round's full trace into long-term memory: old evidence should not shadow new decisions.
3. **Source consistency is a contract.** Order source, decision-trace source and LLM source
   must all agree; tests fail if a real PayPal order is ever labeled with a mock source.
   This is the same honesty rule as the 「○ 规则兜底 / ● 真实 LLM 生成」badge: the demo never
   fabricates provenance.

> Note for the UI: the decision panel shows only *this round's* score breakdown and LLM
> reasoning; the trust profile is an input factor rendered as a separate small card.

## PayPal sandbox accounts

| Account | Email | Role in demo |
|---|---|---|
| **Buyer (Personal)** | `sb-247g1r53552209@personal.example.com` | logs in at the PayPal approval page to confirm/authorize the order (sandbox only, no real money) |
| **Merchant (Business)** | `sb-rw2kp53552213@business.example.com` | used in the PayPal Developer Dashboard to create the app and obtain `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET` |

These are PayPal **sandbox test accounts** - no real funds are involved. Credentials for the
buyer login are displayed in the demo UI itself (PayPal recommended practice for hackathon
judges); secrets for the merchant app belong in `apps/server/.env` and **never in git**.

## Testing

```bash
cd apps/server
python -m pytest -q        # 179 tests
```

Covered: payment-intent routing, authority verdict routing (block / allow_auto /
need_approval), the HITL interrupt gate, approve / reject / modify end-to-end flows
(create -> capture -> explain -> trust write-back), mock vs real payment clients, HTTP API
serialization, capture-failure hardening, plan-snapshot staleness, A/B comparison criteria +
Pareto verdicts, Channel3 retrieval memory, MMR re-ranking + per-card reason codes
(`tests/test_mmr.py`, `tests/test_reason_codes.py`, `tests/test_plan_upgrades.py`),
payment-window lazy expiry + external-fact override (`tests/test_order_expiry.py`), and restart
persistence (`tests/test_restart_persistence.py`: orders + audit timeline are rebuilt from JSONL
after a cold start).

## License

MIT - see [LICENSE](./LICENSE).



