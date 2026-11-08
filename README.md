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
planner uses, drops anything already in the cart or already picked, and shows at most three cards.

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
the comparison rather than contaminating it.

Adding a card to the cart uses the same guardrail as cart editing: over a hard budget it is
**blocked with the cart untouched** (`guardrail.status="blocked"`; `force=true` to override), and it
writes the existing `cart_modified` audit event with
`modification_type="add_from_recommendation"` — no new event type, so the audit event table and
`golden_path_verify.py` stay valid. If Channel3 is unavailable you get an empty list plus a
`disabled`/`error` status, never local-catalog items dressed up as recommendations
(`tests/test_recommendations.py`).

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
python -m pytest -q        # 147 tests
```

Covered: payment-intent routing, authority verdict routing (block / allow_auto /
need_approval), the HITL interrupt gate, approve / reject / modify end-to-end flows
(create -> capture -> explain -> trust write-back), mock vs real payment clients, HTTP API
serialization, capture-failure hardening, plan-snapshot staleness, A/B comparison criteria +
Pareto verdicts, Channel3 retrieval memory, and restart persistence
(`tests/test_restart_persistence.py`: orders + audit timeline are rebuilt from JSONL after a
cold start).

## License

MIT - see [LICENSE](./LICENSE).



