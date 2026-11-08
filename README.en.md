# Agentic Payment - Authorization-Boundary Demo (PayPal Hackathon)

> Canonical README: [README.md](./README.md). This file is kept as a stable link target for the
> Devpost submission; if anything looks stale here, README.md is authoritative.

> **The agent can spend your money - but only inside the authorization boundary you control.**

A LangGraph agent that buys things for you and *explains every decision*: it routes purchase
intent into a 7-node payment subgraph, checks your trust profile + savings-goal constraints,
stops for your approval when needed, creates a real **PayPal sandbox order**, captures it,
then writes A/B-graded evidence back into your trust score.

Zero credentials required to run: `PAYMENT_MODE=mock` runs the full pipeline (approval ->
create -> capture) against a mock PayPal client. Flip to `real` with sandbox credentials and
the same flow hits `api-m.sandbox.paypal.com`.

## Judge path (about 3 minutes, all in the web UI)

The app has three panels: **选购方案 / Decisions**, **审计时间轴 / Audit**, **订单档案 / Orders**.

1. In the request box type: **「社团采购：预算不超过 600 美元，不要塑料，给户外活动用」**
   -> constraints are extracted into the dashboard, and two plans (**A** low-price / **B**
   best-match) appear below with real trade-off numbers.
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

Restarting the API clears the live session but **not** the audit timeline or the order archive:
both are JSONL on disk and are reloaded on startup (see `tests/test_restart_persistence.py`).

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

The payment tab above is *single-purchase* authorization. The app also ships a **group purchase**
flow (ECV 社团采购) where the same principle scales up: **the human holds final transaction
authority at every stage**, and the agent's hooks are mechanically prevented from crossing it.

- **Constraints first, changes explainable** — hard constraints (budget caps, taboos) pre-filter
  the catalog before any strategy runs. Natural-language additions still need the LLM to extract a
  structured constraint, but **delete intents never reach the LLM**: the front end matches your
  words against the existing constraints and only asks the server to `DELETE` a specific one
  (0-hit / multi-hit / add+delete mixes are surfaced, never silently guessed).
- **AI has no payment initiation right** — `create_order` is the *only* payment trigger and it
  must pass HITL review confirmation first: `draft → confirmed` is guarded by an audit record of
  your review, `confirmed → paying` is guarded by a budget PASS, and the amount is **never taken
  from the front-end body** - it is re-derived from the session cart server-side. Repeated clicks
  while paying are idempotent: the same PayPal approval URL is returned, no second order is created.
- **Real-capture reconciliation side door** — `paying → paid` only happens after the PayPal capture
  amount matches the recorded order amount exactly (to the cent). A mismatch is *not* silently forgiven.

## Channel3 Real Product Search (L1 source)

The shopping selection node (`taste_select`) uses **Channel3 SDK** for real product search as the
L1 source, with automatic fallback to the local catalog (K1) when Channel3 is unavailable,
misconfigured, or times out.

- **Channel3 enabled** (`CHANNEL3_API_KEY` set): real products are fetched from
  `https://api.trychannel3.com`, normalized, and returned with `source: "channel3"`.
- **Channel3 unavailable**: empty result triggers fallback to local `catalog_k1` pool, keeping the
  full pay flow functional without external credentials.

This satisfies the "Channel3 real link" requirement — the code path is wired and tested; judges
can flip `CHANNEL3_API_KEY` to a valid key to see live Channel3 products.

## LLM Real Link

The agent uses an LLM for intent parsing (`parse_intent`), authority reasoning
(`authority_check`), and post-payment explanation (`explain`).

- `LLM_MODE=mock` (default): rule-based approximation — no API key needed, fully runnable offline.
- `LLM_MODE=real`: calls a real OpenAI-compatible endpoint (`LLM_BASE_URL`, `LLM_API_KEY`,
  `LLM_MODEL`) for genuine LLM-generated decisions and explanations.

Every LLM-produced field carries an `llm_source` badge (`real` / `mock_rule`) so judges can
visually verify which path produced each output.

## Quick Start

### Prerequisites
Python 3.12+ (recommended 3.12)

### 1. Clone and enter
```bash
git clone https://github.com/Zn3-boop/paypal-for-purchase.git
cd paypal-for-purchase/apps/server
```

### 2. Create venv and install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
```
Edit `.env` — no changes are required to run in mock mode. To test real links:
- Set `PAYMENT_MODE=real` + `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET` for real PayPal sandbox orders.
- Set `CHANNEL3_API_KEY` for real Channel3 product search.
- Set `LLM_MODE=real` + `LLM_API_KEY` for real LLM decisions.

### 4. Run the API
```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
The API runs on port **8000** (FastAPI docs at `http://localhost:8000/docs`).

### 5. Run the web UI (separate terminal)
```bash
cd ../../apps/web
npm install
npm run dev
```
Open `http://localhost:3000` — Vite proxies `/api` to `http://127.0.0.1:8000`.

Make sure `FRONTEND_BASE_URL` in `apps/server/.env` is `http://localhost:3000` (the web dev port)
so PayPal return/cancel redirects land back in the app after approval.

### Docker (one-command)
```bash
docker compose up --build
```
See `docker-compose.yml` at the repo root.

## Demo script (judge talking points)

| Step | Input | What happens |
|------|-------|--------------|
| 1 | 🎯 Set goal: **I want to save for a camera** | `POST /payment/goal` stores a structured `SavingGoal` |
| 2 | **Help me buy a jazz record for Friday night** | enjoyment spend (music >= $20) conflicts with savings goal -> authority verdict `need_approval` (blocked rule) |
| 3 | Click **✓ Approve & Pay** | `POST /payment/approve` resumes the interrupted gate, creates + captures order |
| 4 | Buy a coffee or repeat approval | trust score rises; after enough A-grade approvals small low-risk spends may auto-allow (`allow_auto`) |
| 5 | Click **✏️ Modify & Release** | `modify` decision is treated as A-grade evidence and still captures |

## Technical notes for judges (three design decisions)

1. **Strict response serialization.** We split the LLM SDK response before serializing:
   the typed `parsed` field is exported on its own and the raw response is kept intact for
   tracing. No decision data is lost, and the agent state flow is unchanged.
2. **Evidence has a lifetime, on purpose.** Per-round decision evidence lives in the session
   state and disappears with the session. The only cross-session persistence is the **trust
   score**, written through an A/B-graded evidence gate.
3. **Source consistency is a contract.** Order source, decision-trace source and LLM source
   must all agree; tests fail if a real PayPal order is ever labeled with a mock source.

## PayPal sandbox accounts

| Account | Email | Role in demo |
|-------|-------|--------------|
| **Buyer (Personal)** | `sb-247g1r53552209@personal.example.com` | logs in at the PayPal approval page to confirm/authorize the order (credentials shown in the Payment tab banner - sandbox only, no real money) |
| **Merchant (Business)** | `sb-rw2kp53552213@business.example.com` | used in the PayPal Developer Dashboard to create the app and obtain `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET` |

These are PayPal **sandbox test accounts** - no real funds are involved. Buyer login credentials are
displayed in the demo UI itself. Merchant secrets belong in `apps/server/.env` and **never in git**.

## Testing

```bash
cd apps/server
python -m pytest tests/test_payment_subgraph_integration.py tests/test_payment_http_api.py -q
```

Covered: payment-intent routing, authority verdict routing (block / allow_auto / need_approval),
the HITL interrupt gate, approve / reject / modify end-to-end flows (create -> capture -> explain
-> trust write-back), mock vs real payment clients, HTTP API serialization, and capture-failure
hardening. ~630 tests total in the repo.

## License

MIT - see [LICENSE](./LICENSE).

## Submission checklist (Hackathon)

- [ ] Public repo URL (GitHub / Bitbucket / GitLab)
- [ ] MIT LICENSE file at repo root (detectable on repository "About" section)
- [ ] README (English) with setup + run instructions
- [ ] Test accounts / sandbox credentials in README
- [ ] Demo video (< 3 min) uploaded to YouTube (public)
- [ ] Text description of functionality (English)
- [ ] Channel3 real-link wiring + fallback tests
- [ ] LLM real-link wiring + mock fallback
- [ ] PayPal sandbox real-link wiring + mock fallback