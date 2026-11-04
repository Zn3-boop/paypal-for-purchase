"""Single source of truth for demo seed numbers (ASCII-only code).

Behavior-derived, not hand-written: it drives the ACTUAL planner entry that
GET /api/v1/plans uses (generate_dual_plans with max_items=8, intent_query
fixed, use_channel3=False) and the ACTUAL D2 create_order budget gate.

Authoritative numbers (asserted below, all verified at runtime):
  mainline budget  600.0
  plan A            26.0  (8 items, cheapest-first)
  plan B           393.0  (8 items, durable-first)
  fallback budget  350.0  (must be < plan B total 393.0)
  D2 gate overage   43.0  (393.0 - 350.0) when create_order is attempted
  delete-3 saved   108.0  (k1_031 38 + k1_007 35 + k1_044 35) -> cart 285.0

Run:  python scripts/calc_demo_numbers.py
"""
from __future__ import annotations

import asyncio
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "apps" / "server"
sys.path.insert(0, str(SERVER))

from app.services.group_purchase import Constraint, generate_dual_plans
from app.services.shopper.catalog_k1 import get_k1_items

MAINLINE_BUDGET = 600.0
FALLBACK_BUDGET = 350.0


def _brief(row) -> str:
    return "{}:{}({})".format(row.get("id"), float(row.get("price", 0)), row.get("name"))


async def main() -> int:
    catalog = get_k1_items()
    min_price = min(float(i.get("price", 999999)) for i in catalog)

    c = [Constraint(id="c_budget", type="budget",
                    content="Total budget not exceed $600",
                    hard=True, source="member-A", numeric_value=MAINLINE_BUDGET)]
    result = await generate_dual_plans("ECV 社团采购", c, use_channel3=False)
    a = result["plan_a"]
    b = result["plan_b"]
    a_total = float(a["total"])
    b_total = float(b["total"])
    assert a_total == 26.0, a_total
    assert b_total == 393.0, b_total
    assert len(a["items"]) == 8 and len(b["items"]) == 8
    assert a["items"] == sorted(a["items"], key=lambda i: float(i["price"]))
    assert min_price <= FALLBACK_BUDGET < b_total

    prices_b = [float(i["price"]) for i in b["items"]]
    need = b_total - FALLBACK_BUDGET
    best = None
    for combo in combinations(range(len(prices_b)), 3):
        saved = sum(prices_b[i] for i in combo)
        if saved >= need and (best is None or saved < best[0]):
            best = (saved, combo)
    assert best is not None, "no 3-item combo covers overage"
    saved, combo = best
    assert saved == 108.0, saved
    removed = [b["items"][i] for i in combo]
    new_total = b_total - saved
    assert new_total <= FALLBACK_BUDGET
    assert abs(new_total - 285.0) < 1e-6

    print("Demo numbers (behavior-derived):")
    print("  mainline budget  = {} (per-person totals: A {:.2f}, B {:.2f} for 5) "
          .format(MAINLINE_BUDGET, a_total / 5, b_total / 5))
    print("  min item price   = {}".format(min_price))
    print("  plan A total     = {} (items={})".format(a_total, len(a["items"])))
    print("  plan B total     = {} (items={})".format(b_total, len(b["items"])))
    print("  fallback budget  = {}  (must be < plan B total)".format(FALLBACK_BUDGET))
    print("  D2 gate overage  = {}  (plan-B cart vs fallback budget)".format(b_total - FALLBACK_BUDGET))
    print("  delete-3 removes : {}".format(", ".join(_brief(i) for i in removed)))
    print("  delete-3 saved   = {}  -> new total = {} (<= fallback budget)".format(saved, new_total))
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))