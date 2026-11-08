# Deployment & Test Guide for Judges

This guide tells hackathon judges exactly how to build, run, and test the project locally,
including sandbox credentials, test accounts, and how to verify the Channel3 and LLM real links.

## 1. What this project does

A LangGraph payment agent that buys items for the user but only inside an **authorization boundary
the human controls**. It selects real products (Channel3 SDK, with local fallback), runs a 7-node
payment subgraph (trust profile + savings-goal constraints), stops for human approval when needed,
creates and captures a real **PayPal sandbox order**, and writes A/B-graded evidence back into the
user's trust score. Every LLM decision carries a source badge (`○ Rule fallback / ● Real LLM
generated`) so provenance is never faked.

## 2. One-command run (Docker Compose)

```bash
# at repo root
docker compose up --build
```

- Backend: `http://localhost:8000`
- Frontend: `http://localhost:3000` (if `apps/web` Dockerfile exists)

No API keys are required. Everything runs in **mock mode** by default.

## 3. Manual run (Python)

```bash
cd apps/server
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # edit .env if you want real links
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000/docs` for the FastAPI interactive API, or open the frontend.

## 4. Test accounts & sandbox credentials (no real money)

These are **PayPal sandbox test accounts** — no real funds are involved.

| Account | Email | Role |
|---------|-------|------|
| Buyer (Personal) | `sb-247g1r53552209@personal.example.com` | Log in at the PayPal approval page to confirm/authorize the order (sandbox test account - no real money) |
| Merchant (Business) | `sb-rw2kp53552213@business.example.com` | Used in the PayPal Developer Dashboard to create the app and obtain `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET`. |

Sandbox credentials go in `apps/server/.env` (never commit real secrets):
```
PAYPAL_SANDBOX_CLIENT_ID=your_sandbox_client_id
PAYPAL_SANDBOX_SECRET=your_sandbox_secret
PAYMENT_MODE=real          # flip from mock to hit api-m.sandbox.paypal.com
```

## 5. Verifying the Channel3 real link

The shopping selection node (`taste_select`) calls the Channel3 SDK (`channel3-sdk>=4.5.0`) as the
L1 real product source. If Channel3 is unavailable or misconfigured, it falls back to the local
`catalog_k1` pool so the full pay flow still works.

### How a judge can confirm Channel3 is wired:

1. Read `apps/server/app/services/channel3/client.py` — `search_products_for_query` calls
   `client.products.search(query=..., limit=...)` via the Channel3 SDK and tags each result with
   `"source": "channel3"`.
2. Read `apps/server/tests/test_k2_fallback.py` — tests normal Channel3 flow, timeout fallback to
   local K1, cache hits, and concurrent queries.
3. To see **live** Channel3 products, set a valid key in `.env`:
   ```
   CHANNEL3_API_KEY=your_valid_channel3_key
   ```
   Then search for any product (e.g. "jazz record") and confirm results carry `source: "channel3"`.

Without a key, the endpoint returns `[]` and the server falls back to local catalog — this is
expected and safe for offline judging.

## 6. Verifying the LLM real link

The agent uses an LLM for intent parsing, authority reasoning, and post-payment explanation.

- `LLM_MODE=mock` (default): rule-based approximation — no API key, fully offline.
- `LLM_MODE=real`: calls a real OpenAI-compatible endpoint.

### How a judge can confirm LLM is wired:

1. Read `apps/server/app/core/config.py` — `llm_mode`, `llm_api_key`, `llm_base_url`, `llm_model`.
2. Every reply includes an `llm_source` field: `real` when a real model generated it, `mock_rule`
   when rule-based fallback was used.
3. To see **real** LLM output, set in `.env`:
   ```
   LLM_MODE=real
   LLM_API_KEY=your_openrouter_or_qwen_key
   LLM_BASE_URL=https://openrouter.ai/api/v1   # or your Qwen/DashScope compatible endpoint
   LLM_MODEL=openai/gpt-4o-mini
   ```

## 7. Verifying the PayPal real link

- `PAYMENT_MODE=mock` (default): full approval → create → capture pipeline against a mock PayPal
  client. No credentials needed.
- `PAYMENT_MODE=real`: same flow hits `api-m.sandbox.paypal.com` with sandbox credentials.

Set in `.env`:
```
PAYMENT_MODE=real
PAYPAL_SANDBOX_CLIENT_ID=...
PAYPAL_SANDBOX_SECRET=...
```

## 8. Running the test suite

```bash
cd apps/server
python -m pytest -q        # 132 tests
```

Covered: payment-intent routing, authority verdicts (block / allow_auto / need_approval), HITL
interrupt gate, approve / reject / modify end-to-end, mock vs real payment clients, HTTP API
serialization, capture-failure hardening, Channel3 timeout fallback, plan-snapshot staleness,
A/B comparison criteria + Pareto verdict, Channel3 retrieval memory, and restart persistence
(`tests/test_restart_persistence.py`).

## 9. What judges see in the UI

The web app opens on **🛒 选购方案** (plans) and has two more panels: **📜 审计时间轴** (audit) and
**📦 订单档案** (orders). Nothing is preset — no budget, no constraints, no plans.

1. Type your own request in the box, e.g. 「社团采购：预算不超过 500 美元，不要塑料，给户外活动用」
   → that sentence's budget/taboo appear in the constraint dashboard and two plans render
   (A low-price / B best-match) with the 📐 comparison criteria and the Channel3 evidence line.
2. Click **选这个方案** → confirmation modal: amount, natural-language cart edit, budget guard,
   split-bill preview. Try 「删掉最贵的那件」 and watch the total drop.
3. Confirm → PayPal **sandbox** approval page (log in with the buyer account above) → approve.
4. Back in the app: **订单档案** auto-expands the paid order with its state transitions, and
   **审计时间轴** shows the full chain; ask 「我的订单到哪一步了？」 for a state-machine answer.
5. Optional: restart the API, then reload — the session (constraints / plans / cart), orders, audit
   timeline and 🧠 检索记忆 all come back from disk. Only a pending PayPal approval URL is
   re-created, because that registry is in-memory.

## 10. Submission requirements satisfied

- [ ] Public GitHub / Bitbucket / GitLab repository URL
- [ ] MIT LICENSE at repo root (detectable on repository "About")
- [ ] English README with setup + run instructions (`README.md`)
- [ ] Test accounts + sandbox credentials documented above
- [ ] Demo video (< 3 min) on YouTube (public link in submission form)
- [ ] Text description in English
- [ ] Channel3 real-link wiring + fallback tests
- [ ] LLM real-link wiring + mock fallback
- [ ] PayPal sandbox real-link wiring + mock fallback