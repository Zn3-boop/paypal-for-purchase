# Agentic Payment - Authorization-Boundary Demo (PayPal Hackathon)

> **The agent can spend your money - but only inside the authorization boundary you control.**

A LangGraph agent that buys things for you and *explains every decision*: it routes purchase
intent into a 7-node payment subgraph, checks your trust profile + savings-goal constraints,
stops for your approval when needed, creates a real **PayPal sandbox order**, captures it,
then writes A/B-graded evidence back into your trust score.

Zero credentials required to run: `PAYMENT_MODE=mock` runs the full pipeline (approval ->
create -> capture) against a mock PayPal client. Flip to `real` with sandbox credentials and
the same flow hits `api-m.sandbox.paypal.com`.

## 30-second judge path

1. Open the **Payment tab** (it is the default landing tab). The yellow banner shows the
   **PayPal sandbox buyer account** - log in with it when the approval link opens.
2. Set a savings goal in the 🎯 box: **「我要存钱买相机」** -> *已保存*.
3. Ask: **「帮我买一张周五晚上听的爵士唱片」** -> the agent selects a jazz record, runs the
   authorization engine, and **stops for your approval** with a reason (enjoyment spend
   conflicts with the savings goal -> blocked rule). No order is created yet.
4. Click **✓ 批准并支付** -> a PayPal sandbox order is created (mock or real) and captured.
5. Watch the trust score tick up (A-grade approval evidence), the **可解释性证据** panel
   showing *why*, and the **○ 规则兜底 / ● 真实 LLM 生成** source badge on every reply.

Repeat with small low-trust/low-amount flows and observe the trust score compounding across
sessions (persisted per user via the memory store).

## Why an authorization boundary?

LLMs are great at *deciding*; they are dangerous at *spending*. This demo answers:

- **How much can the agent spend without asking?** -> `ProgressiveAuthzEngine` (trust score +
  auto-threshold; new users require approval on every purchase, thresholds only shrink).
- **What must always ask?** -> user-set savings goals (`GoalMemory`), category-conflict and
  monthly-cap rules, risk checks.
- **How does the user regain control?** -> HITL approval gate (`approve` / `reject` / `modify`),
  and the agent must explain afterwards with graded evidence.

## Architecture

```
Web Frontend (React + Vite) -- PaymentTab -- /payment/agent, /payment/approve, /payment/goal
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
1. Channel3 API (official hackathon sponsor)
2. Local mock dataset, as fallback for stable demo

## Quick start

Requirements: **Python 3.12+**, **Node.js 20+**, **pnpm**.

```bash
# 1. Backend
cd apps/server
python -m venv .venv && .venv\Scripts\activate        # (Windows) / source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                                # (Windows) / cp .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. Frontend (second terminal)
pnpm install
pnpm --dir apps/web dev                               # http://localhost:3000
```

**It runs out of the box with zero external keys** - `PAYMENT_MODE=mock`, `LLM_MODE=mock`
are the safe defaults in `.env.example`, so judges can clone-and-run without credentials.
Open http://localhost:3000 and land on the Payment tab.

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
| **Buyer (Personal)** | `sb-247g1r53552209@personal.example.com` | logs in at the PayPal approval page to confirm/authorize the order (credentials are shown in the Payment tab banner - sandbox only, no real money) |
| **Merchant (Business)** | `sb-rw2kp53552213@business.example.com` | used in the PayPal Developer Dashboard to create the app and obtain `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET` |

These are PayPal **sandbox test accounts** - no real funds are involved. Credentials for the
buyer login are displayed in the demo UI itself (PayPal recommended practice for hackathon
judges); secrets for the merchant app belong in `apps/server/.env` and **never in git**.

## Testing

```bash
cd apps/server
python -m pytest tests/test_payment_subgraph_integration.py tests/test_payment_http_api.py -q
```

Covered: payment-intent routing, authority verdict routing (block / allow_auto /
need_approval), the HITL interrupt gate, approve / reject / modify end-to-end flows
(create -> capture -> explain -> trust write-back), mock vs real payment clients, HTTP API
serialization, and capture-failure hardening. ~630 tests total in the repo.

## License

MIT - see [LICENSE](./LICENSE).



