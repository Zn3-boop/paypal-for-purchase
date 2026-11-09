"""录制前硬门（pre-flight）：两个检查都必须通过才能开始录制。

  1) A/B 不能是两个相同的方案：B 的实现是「同品类里换 match_score 更高的候选」，
     候选池来自 Channel3 —— Channel3 挂了，B 会退化成 A 的复制品，整个对比叙事塌掉；
     因此「B ≠ A」比「推荐区有卡」更硬，只过第二条不能开录。
  2) 推荐泳道必须是 status=ok 且正好 3 张卡。

  用法（在提交一次需求、选过一个方案之后运行）：
      python scripts/preflight_demo.py --session-id <id> [--base http://127.0.0.1:8000]
  退出码 0 = 全部通过；1 = 有硬伤。只读，不写任何数据。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    ok: list[str] = []
    fails: list[str] = []

    try:
        plans = _get(args.base, f"/api/v1/plans?session_id={args.session_id}")
    except urllib.error.HTTPError as e:
        print(f"[FAIL] /api/v1/plans -> HTTP {e.code}（会话不存在？先提交一次需求）")
        return 1

    items_a = (plans.get("plan_a") or {}).get("items") or []
    items_b = (plans.get("plan_b") or {}).get("items") or []
    if not items_a or not items_b:
        fails.append("方案没生成（plan_a/plan_b.items 为空）：先提交需求并生成 A/B。")
    else:
        ids_a = [i.get("id") for i in items_a]
        ids_b = [i.get("id") for i in items_b]
        if ids_a == ids_b:
            fails.append(
                "A 与 B 是同一套商品：没有同品类可升级候选，镜头里 B 会像 A 的复制品。"
            )
        else:
            ok.append(f"A 与 B 确实不同（A {len(items_a)} 件 / B {len(items_b)} 件）")

    upgrades = (plans.get("comparison") or {}).get("upgrades") or []
    if not upgrades:
        fails.append("comparison.upgrades 为空：逐件 diff 讲不出来。")
    else:
        ok.append(f"逐件 diff 可用（{len(upgrades)} 项替换，合计 "
                  f"${sum(u.get('delta') or 0 for u in upgrades):.2f}）")

    try:
        recs = _get(args.base, f"/api/v1/recommendations?session_id={args.session_id}&limit=3")
    except urllib.error.HTTPError as e:
        print(f"[FAIL] /api/v1/recommendations -> HTTP {e.code}")
        return 1

    status = (recs.get("channel3") or {}).get("status")
    shown = int(recs.get("shown") or 0)
    if status != "ok":
        fails.append(f"推荐泳道状态为 {status}（不是 ok）：Channel3 不可用，B 也可能跟着退化。")
    elif shown != 3:
        fails.append(f"推荐泳道只展示了 {shown} 张卡（要求正好 3 张）。")
    else:
        ok.append(f"推荐泳道 ok，3 张卡（查询「{recs.get('query_used')}」→ 命中 "
                  f"{recs.get('search_hits')} 件）")

    for line in ok:
        print(f"  [OK] {line}")
    if fails:
        print("===== 硬门未过，不能开录 =====")
        for line in fails:
            print(f"  [FAIL] {line}")
        return 1
    print("===== 全部通过，可以开录 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())