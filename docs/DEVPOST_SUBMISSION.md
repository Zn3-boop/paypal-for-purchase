# Devpost Submission — Agentic Payment Inside Your Authorization Boundary

**Track:** Agentic Commerce / PayPal sandbox
**Tagline:** The agent can decide, negotiate and explain — but it can only spend inside the
boundary you set, and the last mile of payment always belongs to you.

## The problem

LLM agents are increasingly good at *choosing* what to buy. They are still the wrong entity to
*authorize* spending. Real purchases also happen in groups: several people set hard constraints
(budget caps, taboos), a plan must satisfy all of them, and someone must be accountable for the
final order. Most "AI shopping" demos skip that accountability layer and quietly hard-code a
cart total.

## What we built

A full-stack group-purchase agent on **PayPal sandbox** with five inspectable panels:

1. **约束看板** — four constraint types, conflict highlighting, natural-language add **and
   delete**. Delete intents are resolved deterministically against existing structured
   constraints (0-hit / multi-hit / mixed add+delete are surfaced, never guessed) and never
   sent to an LLM.
2. **方案对比** — hard constraints pre-filter the catalog, then low-price (A) and best-match (B)
   plans are generated with trade-offs, risks and *who was sacrificed*.
3. **对话与支付 / 复核** — natural-language cart edits, split-bill preview, HITL confirmation.
4. **审计时间轴** — append-only JSONL of every decision: constraint change, plan generation,
   cart modification, review confirmation, every state transition, payment completion,
   guardrail failures. The log is written to disk, so the timeline is rebuilt after a restart.
5. **订单档案** — order list, auto-expanded detail from the PayPal success CTA, state-machine
   transitions, item snapshot, related audit events, and **read-only natural-language order
   queries** with server-enforced session ownership. Order records are appended to
   `data/orders/orders.jsonl` and reloaded on startup.

## Technical highlights

1. **Constraint-first, not constraint-flavored.** Budget/taboo hard constraints filter the
   catalog *before* strategy assembly; the planner never "hard-crams" an item to hit a budget.
   When nothing is feasible it returns an explicit `insufficient_budget` / taboo fallback with
   two concrete choices instead of a fake plan.
2. **A guarded order state machine owned by the human.** `draft → confirmed` requires an audit
   record of the user's review; `confirmed → paying` requires a budget PASS; `paying → paid`
   requires the captured amount to equal the order amount **to the cent**. `create_order` is
   the only payment trigger, ignores the front-end amount, and is **idempotent while paying**
   (same approval URL, no duplicate PayPal order). A capture mismatch writes
   `payment_amount_mismatch` and cancels the order instead of silently accepting it.
3. **Explainability as a first-class artifact.** Nine audit event types are emitted from the
   decision points themselves, not reconstructed afterwards. The timeline UI and the order
   detail panel share one event-metadata module, so "what happened" is identical in both views.
4. **Honest degradation, visibly labeled.** Channel3 search failures fall back to the local K1
   catalog (45 items); LLM extraction failures fall back to deterministic rules; both expose a
   source badge so judges never see fabricated provenance.
5. **Read-only natural-language order query.** `POST /orders/query` takes only free text;
   `session_id` is injected server-side, a foreign `ord_xxx` is answered "not found" without
   leaking existence, and the query never mutates the state machine.
6. **The A/B comparison states its own rules and is checkable.** Both plans come from the *same
   candidate pool* (hard constraints applied first, same budget cap, same min/max item count) and
   differ only in objective: A minimizes total price, B maximizes the Channel3 keyword-match score
   sum. The API returns the contract in `comparison.criteria`, the raw numbers in
   `comparison.metrics`, and a **Pareto verdict** in `comparison.pareto` — when one plan is no worse
   on both axes it is reported as *dominated*, not dressed up as a trade-off. The UI prints the same
   lines, so nothing on screen is unsupported by the response.
7. **Channel3 retrieval memory without fake personalization.** Submitted requests and plan
   selections are appended to JSONL and surfaced as a 🧠 检索记忆 row (deduplicated queries +
   last selected plan). Clicking an entry re-runs that exact sentence; history is never merged into
   the current Channel3 query, because pretending last week's taste is this week's intent would be
   fabricated relevance.

## 3-minute demo path

1. Type **your own** request in natural language — nothing is preset: the app starts with no
   constraints and no plans. For example:
   「社团采购：预算不超过 <你填的金额> 美元，不要 <你不想要的材质>，给户外活动用」 → the sentence's own
   budget/taboo are extracted into the dashboard, and dual plans render with real trade-off numbers
   derived from that input.
2. Delete a constraint in natural language: 「把预算约束删掉」 → plan content changes on replan
   (audit shows `constraint_changed/removed`).
3. Modify the cart in natural language, open the confirmation modal → split preview per person.
4. Confirm → PayPal sandbox approval page → capture.
5. Land on **订单档案** via the success CTA → the paid order auto-expands with transitions and
   audit events; ask 「我的订单到哪一步了？」 → status answer from the state machine.
6. Show the audit timeline: constraint → plan → cart → review → confirmed → paying →
   payment_completed → paid.

## Verification evidence (run locally)

- `golden_path_verify.py` (fake gateway): 8-event normal sequence strict-match, plus an
  amount-tampering branch that must produce `payment_amount_mismatch` and a cancelled order;
  snapshot archived under `docs/golden_path_timeline_*.json`.
- Budget guardrail: 1000 cart vs 800 cap → overage 200 + cheaper-replacement suggestions;
  budget 1.0 → no feasible plan + two explicit fallback options.
- `create_order` double-click → same PayPal token/approval URL, gateway called once.
- Order archive/query probes: `session_id` required (422), cross-session access 403, foreign
  order id answered without leaking, polling stops once no order is `paying`.
- Frontend `tsc -b` and `vite build` are green; backend `pytest -q` → **132 passed**
  (`tests/test_restart_persistence.py` covers restart recovery of the order archive and the audit
  timeline; `tests/test_comparison_criteria.py` pins the A/B criteria + Pareto verdict;
  `tests/test_search_memory.py` covers retrieval-memory dedupe and restart survival).

## Honest limitations

- Session state (constraints / cart / plan snapshot) is in-memory (demo-grade): restarting the API
  resets the live session and the pending PayPal approval-URL registry. **Order records and the
  audit timeline are persisted** as JSONL (`data/orders/orders.jsonl`,
  `data/audit_logs/<user>/audit_<date>.jsonl`) and reloaded on startup, so the order archive and
  audit trail survive a restart even though the in-progress session does not.
- Split payment is simulated with a payout schedule (single sandbox merchant account), not real
  multi-recipient payouts.
- The K1 catalog is a curated mock dataset used as an offline fallback for Channel3.

## Built with

Python 3.12 · FastAPI · LangGraph · Pydantic · React · TypeScript · Vite · Channel3 API ·
PayPal Orders v2 (sandbox) · JSONL audit log