"""质量门禁：20 条模板需求 → 真实抽取 → 真实 Channel3 检索 → 逐条判定。

这是把散落在 .tmp/template_audit.py 的临时审计正式化成的工程组件，
同时修掉了原脚本的两个缺陷（2026-11-13）：

1. **预算 prefilter 缺失**（假阳性 unsat）：原脚本拿 Channel3 原始 20 件直接
   evaluate，而真实流水线 `prefilter_by_constraints` 会先把单价 > 预算的商品
   剔除再判 —— 于是「单件预算 ≤ 45」这种约束出现「最高单价 $849 > $45 →
   unsat」，其实是候选池里混进了下雨天用的 patio 伞。本脚本默认模拟
   prefilter（`--no-prefilter` 可关），unsat 才是真问题。
2. **规格数字被判定层丢弃**（316 永远 ❔）：`_extract_specs` 现在输出带材质
   上下文的「316不锈钢」，`satisfaction` 材质分支才能对上 stainless。

用法：
    python scripts/quality_gate.py                      # 人类可读报告
    python scripts/quality_gate.py --json out.json      # 机器可读基线
    python scripts/quality_gate.py --max-unknown 10 --min-sat 85
        # 断言模式：unknown 超过 10 或 sat 低于 85 时退出码 1（CI 可挂）。

需要网络（Channel3 API）。这是线下基线工具，不是解耦单元测试的替代品。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，审计报告含中文/unicode 证据词（如 non‑breaking hyphen
# U+2011）会炸；切到 UTF-8 再输出。
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "server"))

from app.api.constraints import _simple_rule_extraction  # noqa: E402
from app.services.channel3.client import search_products_with_status  # noqa: E402
from app.services.group_purchase.constraints import Constraint  # noqa: E402
from app.services.group_purchase.planner import build_search_query  # noqa: E402
from app.services.group_purchase.satisfaction import evaluate  # noqa: E402

OUT = io.StringIO()


def p(*args: Any) -> None:
    print(*args, file=OUT)


SCENARIOS: list[tuple[str, str]] = [
    # 服饰类
    ("服1 半身裙", "我需要一条半身裙，垂感好，黑色，中长，适合通勤。"),
    ("服2 针织开衫", "我需要一件针织开衫，软糯亲肤，米白色，短款，叠穿内搭。"),
    ("服3 牛仔裤", "我需要一条牛仔裤，高腰版型，水洗蓝，直筒，不容易掉色。"),
    ("服4 皮鞋", "我需要一双皮鞋，哑光皮质，深棕色，低跟，走路不累脚。"),
    # 数码类
    ("数1 充电宝", "我需要一个充电宝，轻薄便携，白色，小容量，出门应急充电。"),
    ("数2 键盘", "我需要一把键盘，静音轴体，浅灰配色，68键，写代码打字用。"),
    ("数3 鼠标", "我需要一个鼠标，人体工学，磨砂黑，无线，打游戏顺手。"),
    ("数4 数据线", "我需要一根数据线，编织外皮，藏青色，一米长，充电传输稳定。"),
    # 生活好物
    ("生1 马克杯", "我需要一只马克杯，陶瓷材质，奶白色，宽口，泡茶不烫手。"),
    ("生2 雨伞", "我需要一把雨伞，抗风骨架，墨绿，折叠款，方便放包里。"),
    ("生3 帆布包", "我需要一个帆布包，厚实布料，卡其色，大号，装书本电脑。"),
    ("生4 围巾", "我需要一条围巾，羊毛混纺，焦糖色，宽版，冬天保暖。"),
    # 团体采购
    ("团1 通勤外套", "我们团体采购一批通勤外套，挺括抗皱，藏青色，中长款，单件预算不超 320，一共采购 28 件，用于团队统一工装。"),
    ("团2 蓝牙耳机", "我们团体采购一批无线蓝牙耳机，音质清晰低延迟，哑光黑，小巧入耳款，单件预算不超 160，一共采购 15 副，发给组员日常办公听会议。"),
    ("团3 双肩背包", "我们团体采购一批双肩背包，耐磨防水面料，深灰色，大号，单件预算不超 180，一共采购 22 个，外出活动统一使用。"),
    ("团4 保温杯", "我们团体采购一批保温杯，316不锈钢，米白色，500ml，单件预算不超 90，一共采购 35 只，作为团队伴手礼。"),
    ("团5 雨伞", "我们团体采购一批雨伞，加固抗风骨架，墨绿，折叠款，单件预算不超 45，一共采购 40 把，团建统一配备。"),
    ("团6 针织围巾", "我们团体采购一批针织围巾，羊毛混纺，酒红色，宽版，单件预算不超 110，一共采购 18 条，冬季团队福利。"),
    ("团7 静音键盘", "我们团体采购一批静音键盘，按键顺滑，浅灰色，68键，单件预算不超 130，一共采购 12 把，分配给开发工位。"),
    ("团8 帆布托特包", "我们团体采购一批帆布托特包，加厚帆布，卡其色，加大款，单件预算不超 35，一共采购 50 个，展会物料包。"),
]


def mk_constraints(query: str) -> list[Constraint]:
    raw = _simple_rule_extraction(query, "demo")
    out = []
    for i, c in enumerate(raw):
        d = dict(c)
        d.setdefault("id", f"c{i}")
        d.pop("status", None)
        out.append(Constraint(**{k: v for k, v in d.items()
                                 if k in {"id", "type", "content", "source", "hard",
                                          "numeric_value", "scope"}}))
    return out


def _prefilter_by_budget(
    items: list[Any], constraints: list[Constraint]
) -> list[Any]:
    """模拟 planner.prefilter_by_constraints 的预算行为：任何硬预算约束下，
    单价 > numeric_value 的商品在方案组装前就被剔除（total 预算也按单件保守剔）。

    原审计脚本缺这步，导致「候选池混进 $849 伞」被误判成预算 unsat。
    """
    caps = [c.numeric_value for c in constraints
            if c.type == "budget" and c.status == "active" and c.numeric_value is not None]
    if not caps:
        return list(items)
    ceiling = min(caps)
    kept: list[Any] = []
    for it in items:
        price = it.get("price") if isinstance(it, dict) else getattr(it, "price", None)
        if float(price or 0) <= ceiling:
            kept.append(it)
    return kept


async def audit(
    name: str, query: str, sem: asyncio.Semaphore, prefilter: bool
) -> dict:
    cons = mk_constraints(query)
    sq, _dp, _a = build_search_query(query)
    async with sem:
        result = await search_products_with_status(sq, limit=20)
    raw_items = list(result.items or [])
    items = _prefilter_by_budget(raw_items, cons) if prefilter else raw_items
    res = evaluate(cons, items) if items else {"constraints": [], "passed": 0, "total": len(cons)}
    return {"name": name, "query": query, "sq": sq.split(" | ")[0], "status": result.status,
            "raw_n": len(raw_items), "n": len(items),
            "verdicts": res.get("constraints") or [],
            "headline": res.get("headline") or ""}


async def run_all(prefilter: bool) -> list[dict]:
    sem = asyncio.Semaphore(4)
    rows = await asyncio.gather(
        *[audit(n, q, sem, prefilter) for n, q in SCENARIOS],
        return_exceptions=True,
    )
    return [r for r in rows if not isinstance(r, Exception)]
def _stats(rows: list[dict]) -> dict[str, int]:
    stats = {"sat": 0, "unsat": 0, "unknown": 0}
    for row in rows:
        for v in row["verdicts"]:
            st = v.get("status")
            if st in stats:
                stats[st] += 1
    return stats


def render(rows: list[dict], stats: dict[str, int]) -> str:
    buf = io.StringIO()
    for row in rows:
        buf.write(f"\n{'=' * 72}\n{row['name']}  [{row['status']}] 命中 {row['n']}/{row['raw_n']} 件")
        buf.write(f"\n  Q: {row['query']}\n  检索: {row['sq']}\n  判定: {row['headline']}")
        for v in row["verdicts"]:
            st = v.get("status")
            mark = {"sat": "OK ", "unsat": "NG!", "unknown": "?  "}.get(st, "?  ")
            detail = (v.get("evidence") or v.get("reason") or "").strip()
            buf.write(f"\n    {mark} [{v.get('type'):10}|{str(v.get('scope')):8}] "
                      f"{v.get('content')!r:22} :: {detail}")
    buf.write(f"\n\n{'=' * 72}\nTOTAL sat={stats['sat']} unsat={stats['unsat']} unknown={stats['unknown']}")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="PATH", help="同时把结构化基线写到该文件")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="关掉预算 prefilter（复现旧审计的原始候选池判定）")
    ap.add_argument("--max-unknown", type=int, default=None,
                    help="断言：unknown 判定数超过该值 → 退出码 1")
    ap.add_argument("--min-sat", type=int, default=None,
                    help="断言：sat 判定数低于该值 → 退出码 1")
    args = ap.parse_args()

    rows = asyncio.run(run_all(prefilter=not args.no_prefilter))
    stats = _stats(rows)
    print(render(rows, stats))

    if args.json:
        payload = {
            "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "prefilter": not args.no_prefilter,
            "stats": stats,
            "scenarios": [
                {
                    "name": r["name"],
                    "status": r["status"],
                    "raw_n": r["raw_n"],
                    "n": r["n"],
                    "search_query": r["sq"],
                    "verdicts": [
                        {"type": v.get("type"), "scope": v.get("scope"),
                         "content": v.get("content"), "status": v.get("status")}
                        for v in r["verdicts"]
                    ],
                }
                for r in rows
            ],
        }
        out_path = Path(args.json)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n基线已写入 {out_path}")

    violated: list[str] = []
    if args.max_unknown is not None and stats["unknown"] > args.max_unknown:
        violated.append(f"unknown={stats['unknown']} > 阈值 {args.max_unknown}")
    if args.min_sat is not None and stats["sat"] < args.min_sat:
        violated.append(f"sat={stats['sat']} < 阈值 {args.min_sat}")
    if violated:
        print(f"\n[FAIL] 质量门禁未过：{'；'.join(violated)}")
        return 1
    print("\n[OK] 质量门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())