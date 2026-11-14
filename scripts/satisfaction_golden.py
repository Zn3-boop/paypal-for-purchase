"""离线判定自检：6 个现场 Bad Case → 修复 → 现在实判（Golden Set，可挂 CI）。

为什么需要这个脚本（对照 2026-11-14 现场复盘）：
    quality_gate.py 跑的是真网链路，需要 Channel3 API；面试/评审现场断网就
    复现不了「判定诚实性」。本脚本是**纯函数**（复用 satisfaction.evaluate），
    把 6 个现场 Bad Case 的「修复前表现 → 根因 → 商品证据」固化成 golden set，
    每次运行 3 秒出结果，离线可复算 —— 这是把「我们复盘过现场失败」变成
    评委可以亲手按的表。

对照那位同学的医疗 RAG 项目方法论（评测集 + 对照实验 + Bad Case 迭代）：
    6 个现场 Bad Case 就是我们自己的 "Bad Case 迭代" 记录；每一次修复都
    先写 failing 用例（tests/test_channel3_field_extraction.py），再改
    satisfaction.py，最后收进本 golden set 防止回归。

用法：
    python scripts/satisfaction_golden.py                # PASS/FAIL 表（含修复前对照）
    python scripts/satisfaction_golden.py --json out.json  # 机器可读（CI / 存档）

退出码：任一 golden 场景实际判定 ≠ 期望 → 1（CI 可挂）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Windows 控制台默认 GBK，输出含 unicode 证据词会炸；切 UTF-8。
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "server"))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")

from app.services.channel3.client import _normalize_product  # noqa: E402
from app.services.group_purchase.constraints import Constraint  # noqa: E402
from app.services.group_purchase.satisfaction import evaluate  # noqa: E402

OUT = io.StringIO()


def p(*args: Any) -> None:
    print(*args, file=OUT)


def _product(**kw) -> SimpleNamespace:
    """轻量 Product 替身：_normalize_product 只 getattr，pydantic 必填字段不必要。

    与 tests/test_channel3_field_extraction.py 的 _product 同一构造，保证
    golden set 和回归测试跑的是同一份「真实商品结构」。
    """
    base = {
        "id": "golden_t1",
        "title": "",
        "description": None,
        "brands": None,
        "images": None,
        "category": None,
        "offers": None,
        "key_features": None,
        "materials": None,
        "structured_attributes": None,
        "colors": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _offer(price: float, currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(
        url="https://merchant.example/p/1", domain="merchant.example",
        price=SimpleNamespace(price=price, currency=currency),
    )


def _item(name: str, price: float, desc: str, materials=None, attrs=None, features=None):
    return _normalize_product(_product(
        id="ch3_golden",
        title=name,
        description=desc,
        category=SimpleNamespace(title="Test Category", slug="test"),
        offers=[_offer(price)],
        key_features=features,
        materials=materials or [],
        structured_attributes=attrs or {},
        images=[SimpleNamespace(url="https://img/x.jpg")],
    ))


def _c(idx: int, ctype: str, content: str, num=None, scope="total") -> Constraint:
    return Constraint(id=f"c{idx}", type=ctype, content=content, hard=True,
                      source="demo", numeric_value=num, scope=scope)


# ── Golden Set：name → (商品, 约束, 期望判定, 修复前表现, 根因) ──────
# 商品与约束直接复用 tests/test_channel3_field_extraction.py 里验过的真实结构；
# 「修复前表现」与「根因」是 2026-11-14 现场复盘记录（bad case 迭代叙事）。
GOLDEN: list[dict[str, Any]] = [
    {
        "name": "充电宝 10000mAh 假阳性",
        "before": "❌ 假阳性：key_features 家族描述『10000mAh』命中即 ✅，标题主容量 30000mAh 被忽略",
        "root": "容量数值语义缺失：只看文本子串命中，不看『标题主容量』",
        "item": _item(
            "30000mAh Fast Charging Power Bank PD18W Portable Charger", 74.09,
            "SUPER FAST CHARGING: 10000mAh supports PD18W fast charging, "
            "while 20000mAh/30000mAh support PD22.5W.",
            materials=["plastic"],
            features=["10000mAh supports PD18W fast charging"],
        ),
        "constraints": [_c(1, "functional", "10000mAh")],
        "expect": "sat",
    },
    {
        "name": "充电宝 2000mAh < 需求 10000mAh",
        "before": "❔ 无法判定：商品自称容量但低于需求，无显式『10000mAh』文本",
        "root": "容量语义缺失：商品自述容量 < 需求 → 应确证 unsat",
        "item": _item(
            "2000mAh Slim Pocket Power Bank", 9.9,
            "Great for topping up earbuds between charges.",
            materials=["plastic"],
        ),
        "constraints": [_c(1, "functional", "10000mAh")],
        "expect": "unsat",
    },
    {
        "name": "快充词表",
        "before": "❔ 词表只有 charger/充电，『快充』恒 unknown",
        "root": "SYNONYM 缺 fast charging / quick charge / super fast charging",
        "item": _item(
            "30000mAh Fast Charging Power Bank PD18W Portable Charger", 74.09,
            "SUPER FAST CHARGING: 10000mAh supports PD18W fast charging.",
            materials=["plastic"],
        ),
        "constraints": [_c(1, "functional", "快充")],
        "expect": "sat",
    },
    {
        "name": "保温杯 500ml（40oz ≈ 1183ml）",
        "before": "❔ 文本无『500ml』→ unknown",
        "root": "容量数值语义缺 oz→ml 换算",
        "item": _item(
            "Wellness 40oz. Double Wall Vine Stripe Travel Cup", 14.99,
            "Double-wall insulated travel cup, spill-resistant lid.",
            materials=["stainless steel"],
        ),
        "constraints": [_c(1, "functional", "500ml")],
        "expect": "sat",
    },
    {
        "name": "皮鞋『鞋』0/4",
        "before": "❔ 4 件候选全 unknown：名称只写 PUMP 不写 shoes",
        "root": "『鞋』词表缺 pump/oxford/loafer/heel/mary jane",
        "item": _item(
            "LOW PUCKER PUMP 45", 1400.0,
            "Shiny oxblood leather finish / Rounded toe / Elasticated topline",
            materials=["leather"],
        ),
        "constraints": [_c(1, "functional", "鞋")],
        "expect": "sat",
    },
    {
        "name": "皮鞋『皮鞋』",
        "before": "❔ 无 shoes 字样的 leather product 判不了",
        "root": "缺 leather finish / leather upper / genuine leather 整词",
        "item": _item(
            "LOW PUCKER PUMP 45", 1400.0,
            "Shiny oxblood leather finish / Rounded toe / Elasticated topline",
            materials=["leather"],
        ),
        "constraints": [_c(1, "functional", "皮鞋")],
        "expect": "sat",
    },
    {
        "name": "哑光反证（商品自述 Shiny）",
        "before": "❔ 无法判定：遮光约束没有反证机制",
        "root": "遮光类约束缺『商品自述亮面 → 确证违反』",
        "item": _item(
            "LOW PUCKER PUMP 45", 1400.0,
            "Shiny oxblood leather finish / Rounded toe / Elasticated topline",
            materials=["leather"],
        ),
        "constraints": [_c(1, "functional", "哑光")],
        "expect": "unsat",
    },
]


def _row(verdict: dict, expect: str) -> dict:
    return {
        "expect": expect,
        "actual": verdict["status"],
        "rule": verdict.get("rule", ""),
        "evidence": (verdict.get("evidence") or verdict.get("reason") or "").strip(),
    }


def run_all() -> list[dict]:
    rows = []
    for g in GOLDEN:
        item = g["item"]
        verdict = evaluate(g["constraints"], [item])["constraints"][0]
        rows.append({
            "name": g["name"],
            "before": g["before"],
            "root": g["root"],
            **_row(verdict, g["expect"]),
            "ok": verdict["status"] == g["expect"],
        })
    return rows


def render(rows: list[dict]) -> str:
    buf = io.StringIO()
    for r in rows:
        mark = "✅" if r["ok"] else "❌"
        buf.write(f"\n{'=' * 74}\n{mark} [{r['expect']}] {r['name']}"
                  f"（实判 {r['actual']}）\n")
        buf.write(f"  修复前: {r['before']}\n")
        buf.write(f"  根因  : {r['root']}\n")
        buf.write(f"  实判  : rule={r['rule']} | {r['evidence']}\n")
    passed = sum(1 for r in rows if r["ok"])
    buf.write(f"\n{'=' * 74}\nGOLDEN {passed}/{len(rows)} 通过"
              f"（{len(rows) - passed} 个失败）\n")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="PATH", help="同时写入机器可读结果（CI/存档）")
    args = ap.parse_args()

    rows = run_all()
    print(render(rows))

    if args.json:
        payload = {
            "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "summary": {"passed": sum(1 for r in rows if r["ok"]), "total": len(rows)},
            "cases": rows,
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入 {args.json}")

    failed = [r for r in rows if not r["ok"]]
    if failed:
        print(f"\n[FAIL] {len(failed)} 个 golden 场景实际判定与期望不符"
              f"：{', '.join(r['name'] for r in failed)}")
        return 1
    print("\n[OK] 判定诚实性 golden set 全部通过（离线，3 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())