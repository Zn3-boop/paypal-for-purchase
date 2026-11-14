"""satisfaction.py 纯函数判定层 —— 「✅/❌/❔」必须是可复算的真话。

覆盖录制现场会当面问的三个问题：
1. 预算到底算没算对：$285 ≤ $350 → sat，证据串可直接读；
2. 「打不出 ✅ 的约束」怎么办：主观词（"要好一点"）→ unknown 而不是假装 ✅；
3. 「共 0 条」的根因还在不在：matched 是算出来的，不是写死的。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.satisfaction import (
    RULE_NO_EVIDENCE,
    STATUS_SAT,
    STATUS_UNKNOWN,
    STATUS_UNSAT,
    evaluate,
    item_satisfies,
)


def _c(
    ctype: str,
    content: str,
    *,
    source: str | None = None,
    hard: bool = False,
    numeric_value: float | None = None,
    cid: str | None = None,
    scope: str | None = None,
) -> Constraint:
    return Constraint(
        id=cid or f"c-{ctype}-{content}",
        type=ctype,
        content=content,
        source=source,
        hard=hard,
        numeric_value=numeric_value,
        status="active",
        scope=scope,
    )


def _item(item_id: str, name: str, price: float, tags: list[str] | None = None) -> dict:
    return {"id": item_id, "name": name, "category": "通用", "price": price, "tags": tags or []}


class TestBudget:
    def test_within_budget_sat_with_evidence(self):
        items = [_item("a", "水杯", 95), _item("b", "T恤", 98), _item("c", "帽子", 92)]
        res = evaluate([_c("budget", "总预算 350", hard=True, numeric_value=350)], items)
        assert res["passed"] == 1
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert res["constraints"][0]["evidence"] == "$285.00 ≤ $350.00"

    def test_over_budget_unsat_with_shortfall(self):
        items = [_item("a", "水杯", 200), _item("b", "T恤", 193)]
        res = evaluate([_c("budget", "总预算 350", hard=True, numeric_value=350)], items)
        assert res["constraints"][0]["status"] == STATUS_UNSAT
        assert "超支 $43.00" in res["constraints"][0]["evidence"]

    def test_budget_is_combo_level_not_per_item(self):
        items = [_item("a", "水杯", 95)]
        res = evaluate([_c("budget", "总预算 100", hard=True, numeric_value=100)], items)
        assert res["items"][0]["results"][0]["status"] == "n/a"

    # ── Bug 1（评审坑 3）：单件预算 scope=per_item 必须逐件判定 ──────────
    # 旧实现把「单件预算」的 numeric_value 当组合 cap：8 件 × 130 必然超 → 全盘不可行；
    # 若只拿 budget_limit 又不判逐件，「12 件全过 130」会被误判可行——比报错更糟。
    # 正确语义：per_item 看 max(price) ≤ limit（组合级 verdict），
    #           逐件表里每件单价 ≤ limit → pass，否则 fail。
    def test_per_item_budget_judges_worst_single_price(self):
        items = [_item("a", "键盘", 99), _item("b", "鼠标", 130)]
        res = evaluate(
            [_c("budget", "单件预算不超 130", hard=True, numeric_value=130, scope="per_item")],
            items,
        )
        v = res["constraints"][0]
        assert v["scope"] == "per_item"
        assert v["status"] == STATUS_SAT, v["evidence"]
        assert "单价" in v["evidence"]
        # 逐件表：键盘 pass（99 ≤ 130）、鼠标 pass（130 ≤ 130）
        assert [r["status"] for r in res["items"][0]["results"]] == ["pass"], res["items"]
        assert [r["status"] for r in res["items"][1]["results"]] == ["pass"], res["items"]

    def test_per_item_budget_fails_when_any_item_over(self):
        # 单件 131 超 130，即使总价 230 远超便宜也不该当「组合超支」判——是逐件违规
        items = [_item("a", "键盘", 99), _item("b", "鼠标", 131)]
        res = evaluate(
            [_c("budget", "单件预算不超 130", hard=True, numeric_value=130, scope="per_item")],
            items,
        )
        v = res["constraints"][0]
        assert v["status"] == STATUS_UNSAT, v["evidence"]
        assert "最高单价" in v["evidence"]
        # 逐件表：键盘 pass（99 ≤ 130）、鼠标 fail（131 > 130）
        assert [r["status"] for r in res["items"][0]["results"]] == ["pass"], res["items"]
        assert [r["status"] for r in res["items"][1]["results"]] == ["fail"], res["items"]

    def test_total_budget_is_sum_not_per_item(self):
        # total 语义回归：3 件各 95 总 285 ≤ 350 → SAT；逐件不因单件 > 100 判 fail
        items = [_item("a", "水杯", 95), _item("b", "T恤", 95), _item("c", "帽子", 95)]
        res = evaluate(
            [_c("budget", "总预算 350", hard=True, numeric_value=350, scope="total")],
            items,
        )
        v = res["constraints"][0]
        assert v["scope"] == "total"
        assert v["status"] == STATUS_SAT, v["evidence"]
        # total 预算逐件表一律 n/a（证据是组合级，不复写到每件上贬值）
        # evaluate 的 items[].results 是「每条约束 1 行」，这里单约束 → 每件 1 行 n/a
        for row in res["items"]:
            assert [r["status"] for r in row["results"]] == ["n/a"], res["items"]

    def test_budget_without_amount_is_unknown(self):
        res = evaluate([_c("budget", "预算别太贵")], [_item("a", "水杯", 95)])
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN


class TestFunctional:
    def test_keyword_hit_sat(self):
        items = [_item("a", "无线耳机", 99, tags=["audio", "wireless"]), _item("b", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert "无线耳机" in res["constraints"][0]["evidence"]

    def test_keyword_miss_is_unknown_not_unsat(self):
        # 缺证据 ≠ 违反：目录没覆盖 / 描述缺字段都可能造成"没匹配到"，
        # 因此未命中判 unknown（rule=common.missing_evidence），不冤枉 ❌。
        items = [_item("a", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机")], items)
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN
        assert res["constraints"][0]["rule"] == RULE_NO_EVIDENCE
        assert res["constraints"][0]["scope"] == "combo"

    def test_item_level_pass_fail(self):
        items = [_item("a", "无线耳机", 99), _item("b", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机")], items)
        assert res["items"][0]["results"][0]["status"] == "pass"
        assert res["items"][1]["results"][0]["status"] == "fail"

    # ── 演示场景实证（2026-11-10）：中文约束词 vs 英文商品标签 ──────────────
    def test_key_layout_requires_exact_key_count(self):
        # 「68 键」必须按数字精确匹配：78 键键盘不能被判成 68 键满足（假 ✅ 比 ❔ 更危险）
        items = [_item("a", "Bs8772 Wireless Keyboard", 49.99,
                       tags=["75% layout keyboard with 78 low-profile keys"]),
                 _item("b", "Tofu68 Mechanical Keyboard", 90.0,
                       tags=["68-key layout", "Gateron linear switches"])]
        res = evaluate([_c("functional", "68 键")], items)
        v = res["constraints"][0]
        assert v["status"] == STATUS_SAT, v
        assert "68-key" in (v["evidence"] or "")

    def test_key_layout_unknown_when_no_matching_count(self):
        items = [_item("a", "Bs8772 Wireless Keyboard", 49.99,
                       tags=["75% layout keyboard with 78 low-profile keys"])]
        res = evaluate([_c("functional", "68 键")], items)
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN

    def test_compound_chinese_constraint_expands_to_english_tags(self):
        # 「厚实布料」是整词，词典 key 是「厚实」→ 必须靠子串合并波命中 rugged/thick
        items = [_item("a", "Deluxe Canvas Book Bag", 285.99,
                       tags=["Rugged 15 oz. canvas construction", "cotton canvas"])]
        res = evaluate([_c("functional", "厚实布料")], items)
        v = res["constraints"][0]
        assert v["status"] == STATUS_SAT, v

    def test_khaki_is_judged_as_color_attribute(self):
        # 「卡其色」是新加的颜色词：必须走颜色分支判 sat/unknown，而不是掉进
        # preference.unverifiable（"约束类型 preference 没有可结构化比对的字段"）
        items = [_item("a", "Canvas Bag", 20.0, tags=["Large capacity tote bag", "Khaki cotton canvas"])]
        res = evaluate([_c("preference", "卡其色")], items)
        v = res["constraints"][0]
        assert v["status"] == STATUS_SAT, v
        assert v["rule"] == "attribute.color"


class TestTaboo:
    def test_taboo_hit_unsat(self):
        items = [_item("a", "塑料杯", 30, tags=["plastic", "一次性"])]
        res = evaluate([_c("taboo", "不要塑料", source="成员 A")], items)
        assert res["constraints"][0]["status"] == STATUS_UNSAT
        assert res["constraints"][0]["source"] == "成员 A"
        assert "塑料杯" in res["constraints"][0]["evidence"]

    def test_taboo_clean_sat(self):
        items = [_item("a", "不锈钢杯", 89, tags=["stainless"])]
        res = evaluate([_c("taboo", "不要塑料")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT


class TestEco:
    def test_eco_tag_hit_sat(self):
        items = [_item("a", "帆布袋", 25, tags=["recycled", "环保"])]
        res = evaluate([_c("preference", "要环保")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert "标签命中环保" in res["constraints"][0]["evidence"]

    def test_eco_miss_is_unknown_not_unsat(self):
        # Channel3 没有 eco_label，标签里也没核对到 → 缺证据 ≠ 违反。
        # 商品描述没写"环保"不代表它不环保，判 unknown 让用户人工确认，比假装 ❌ 诚实。
        items = [_item("a", "普通T恤", 20, tags=["cotton"])]
        res = evaluate([_c("preference", "要环保")], items)
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN


class TestUnknown:
    def test_subjective_quality_never_checkmarked(self):
        items = [_item("a", "无线耳机", 99)]
        res = evaluate([_c("preference", "要好一点")], items)
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN
        assert res["constraints"][0]["evidence"] is None

    def test_subjective_goes_to_unknown_count_not_passed(self):
        items = [_item("a", "无线耳机", 99)]
        res = evaluate(
            [
                _c("preference", "要好一点"),
                _c("budget", "预算 300", hard=True, numeric_value=300),
            ],
            items,
        )
        assert res["passed"] == 1
        assert res["unknown"] == 1
        assert "满足 1/2 条" in res["headline"]
        assert "1 条无法判定" in res["headline"]


class TestAggregation:
    def test_headline_counts(self):
        items = [_item("a", "无线耳机", 95, tags=["recycled"])]
        constraints = [
            _c("budget", "预算 350", hard=True, numeric_value=350),
            _c("functional", "需要耳机"),
            _c("preference", "要环保"),
        ]
        res = evaluate(constraints, items)
        assert res["total"] == 3
        assert res["passed"] == 3
        assert res["unknown"] == 0
        assert "满足 3/3 条" in res["headline"]

    def test_max_two_failures_shown(self):
        # unsat 只有两种确证情形：预算超支 / 禁忌词出现。用 3 条禁忌词约束
        # 压到折叠逻辑：超过 2 条不满足时折叠展示，但数据不丢。
        items = [_item("a", "塑料杯", 30, tags=["plastic"]), _item("b", "玻璃杯", 50, tags=["glass"])]
        constraints = [
            _c("taboo", "不要塑料"),
            _c("taboo", "不要玻璃"),
            _c("budget", "总预算 20", hard=True, numeric_value=20),
        ]
        res = evaluate(constraints, items)
        assert res["failed"] == 3
        assert res["max_failures_shown"] == 2
        shown = [c for c in res["constraints"] if c["visible"]]
        assert len(shown) == 2
        assert len(res["hidden_failures"]) == 1

    def test_no_constraints_not_judgeable(self):
        res = evaluate([], [_item("a", "T恤", 20)])
        assert res["judgeable"] is False
        assert res["headline"] == "还没有可判定的约束"

    def test_inactive_constraints_excluded(self):
        c = Constraint(
            id="c-old", type="functional", content="需要耳机",
            status="removed", source=None, hard=False,
        )
        res = evaluate([c], [_item("a", "T恤", 20)])
        assert res["judgeable"] is False


class TestItemSatisfies:
    def test_item_satisfies_positive(self):
        c = _c("functional", "需要耳机")
        assert item_satisfies(c, _item("a", "无线耳机", 99)) is True
        assert item_satisfies(c, _item("b", "T恤", 20)) is False
class TestRulesTable:
    """P2 新增：规则表 RULES + 判定的 rule/scope/limit 字段。"""

    def test_verdicts_carry_rule_from_rules_table(self):
        from app.services.group_purchase.satisfaction import RULES

        # 每条约束都用**能命中**的商品，验证 rule 指向正确的规则条目。
        # scope：预算无单件字样默认 total（Bug 1 后词汇）；其余类型组合层判定 = combo。
        cases = [
            (_c("budget", "预算 200", hard=True, numeric_value=200),
             [_item("a", "无线耳机", 99)], "budget.balance", STATUS_SAT, 200.0, "total"),
            (_c("functional", "需要耳机"),
             [_item("a", "无线耳机", 99)], "functional.keyword_any", STATUS_SAT, None, "combo"),
            (_c("taboo", "不要玻璃"),
             [_item("a", "无线耳机", 99)], "taboo.forbidden_present", STATUS_SAT, None, "combo"),
            (_c("preference", "要环保"),
             [_item("a", "帆布袋", 25, tags=["recycled"])], "eco.label_evidence", STATUS_SAT, None, "combo"),
            (_c("preference", "要好一点"),
             [_item("a", "无线耳机", 99)], "preference.unverifiable", STATUS_UNKNOWN, None, "combo"),
        ]
        for c, items, rule, status, limit, scope in cases:
            v = evaluate([c], items)["constraints"][0]
            assert v["rule"] == rule, f"{c.content}: {v['rule']} != {rule}"
            assert v["status"] == status, f"{c.content}: status {v['status']}"
            assert v["scope"] == scope, f"{c.content}: scope {v['scope']} != {scope}"
            assert v["limit"] == limit, f"{c.content}: limit {v['limit']}"
            # 规则表里必须有该类型的条目（自解释、可复算）
            assert v["type"] in RULES

    def test_material_miss_is_unknown(self):
        items = [_item("a", "普通T恤", 20)]
        res = evaluate([_c("functional", "要不锈钢")], items)
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN

    def test_material_hit_sat(self):
        items = [_item("a", "不锈钢杯", 89, tags=["stainless"])]
        res = evaluate([_c("functional", "要不锈钢")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert res["constraints"][0]["evidence"]  # 命中带证据

    def test_budget_limit_exposed_on_verdict(self):
        items = [_item("a", "水杯", 95)]
        res = evaluate([_c("budget", "总预算 100", hard=True, numeric_value=100)], items)
        assert res["constraints"][0]["limit"] == 100.0

    def test_unknown_not_counted_as_failed(self):
        items = [_item("a", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机"), _c("preference", "要好一点")], items)
        assert res["failed"] == 0
        assert res["unknown"] == 2
        assert res["passed"] == 0


class TestTemplateSynonymCoverage:
    """2026-11-10 20 条模板全量审计：中文约束词必须能对上英文商品证据词。

    根因：检索词典（planner._ZH_TO_EN_QUERY）与判定词典（SYNONYM_KEYWORDS）
    长期是两套。雨伞在检索侧认识 umbrella、判定侧不认识 —— 结果是「检索对了、
    取证全 ❔」，用户看到一堆商品却一条都证明不了，等于没查。这里把每条补齐的
    映射钉死，防止以后再漂移。
    """

    FUNCTIONAL_CASES = [
        ("雨伞", "Travel Umbrella Compact"),
        ("数据线", "Braided USB-C Cable"),
        ("开衫", "Ribbed Knit Cardigan"),
        ("针织", "Garter Knit Wool Scarf"),
        ("折叠", "Collapsible Travel Umbrella"),
        ("抗风", "Windproof Umbrella with Vented Canopy"),
        ("防水", "Water Repellent Backpack"),
        ("保温", "Insulated Stainless Tumbler"),
        ("人体工学", "Ergonomic Optical Mouse"),
        ("加固", "Reinforced Frame Umbrella"),
        ("耐磨", "Abrasion Resistant Backpack"),
        ("编织", "Nylon Braided Charging Cable"),
        ("宽口", "Wide Mouth Ceramic Mug"),
        ("垂感", "Draped Midi Skirt"),
        ("低跟", "Low Heel Leather Pump"),
        ("加大", "Extra Large Canvas Tote"),
        ("小容量", "Compact 5000mAh Power Bank"),
        ("蓝牙", "Bluetooth Earbuds"),
    ]

    def test_functional_synonyms_hit(self):
        for constraint_word, product_name in self.FUNCTIONAL_CASES:
            items = [_item("a", product_name, 50)]
            v = evaluate([_c("functional", constraint_word)], items)["constraints"][0]
            assert v["status"] == STATUS_SAT, (
                f"{constraint_word} 未能匹配 {product_name!r}：{v['reason']}"
            )

    def test_color_synonyms_hit(self):
        cases = [
            ("藏青", "Navy Blue Jacket"),
            ("墨绿", "Forest Green Umbrella"),
            ("浅灰", "Light Grey Keyboard"),
            ("磨砂黑", "Matte Black Mouse"),
            ("哑光黑", "Matte Black Headphones"),
            ("焦糖色", "Caramel Wool Scarf"),
        ]
        for color, product_name in cases:
            v = evaluate([_c("preference", color)], [_item("a", product_name, 50)])["constraints"][0]
            assert v["status"] == STATUS_SAT, f"{color} 未能匹配 {product_name!r}"
            assert v["rule"] == "attribute.color"

    def test_style_mid_length_hits(self):
        # 抽取层常把「中长款」的「款」省掉 → content=中长；key 必须能对上。
        v = evaluate([_c("preference", "中长")], [_item("a", "Longline Wool Coat", 300)])["constraints"][0]
        assert v["status"] == STATUS_SAT
        assert v["rule"] == "attribute.style"

    def test_wool_material_key_hits(self):
        # 「羊毛材质」过去走不到材质分支（MATERIAL_KEYWORDS 只有「呢绒」key）→ 永远 ❔。
        v = evaluate([_c("preference", "羊毛材质")], [_item("a", "Merino Wool Scarf", 60)])["constraints"][0]
        assert v["status"] == STATUS_SAT
        assert v["rule"] == "material.keyword_any"

    def test_leather_material_key_hits(self):
        # 「哑光皮质」→ 「皮质」key（皮革/真皮/leather 都收），否则永远 ❔。
        v = evaluate([_c("preference", "哑光皮质")], [_item("a", "Matte Leather Loafers", 120)])["constraints"][0]
        assert v["status"] == STATUS_SAT
        assert v["rule"] == "material.keyword_any"

    def test_steel_grade_316_hits_stainless(self):
        # 2026-11-13 审计：裸「316」被 _keywords 的 isdigit() 过滤 →「没有可比对的关键词」。
        # 修复后抽取层输出「316不锈钢」，材质分支（`不锈钢 in content`）能对上 stainless。
        v = evaluate(
            [_c("functional", "316不锈钢")],
            [_item("a", "Insulated Quick-Open Stainless Steel Tumbler", 54)],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert v["rule"] == "material.keyword_any"

    def test_matte_uk_spelling_matt_hits(self):
        # 2026-11-13 审计：商品名写「Matt New Kilt Shoes」（英式 matt），词典只有 matte → ❔。
        v = evaluate([_c("functional", "哑光")], [_item("a", "Matt New Kilt Shoes in Dark Brown", 89)])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "matt" in v["evidence"], v["evidence"]

    def test_500ml_matches_space_and_no_space(self):
        # 容量数字的中空格是排版差异，不该决定能不能核对。
        for name in ("Insulated Bottle 500 ml", "Insulated Bottle 500ml"):
            v = evaluate([_c("functional", "500ml")], [_item("a", name, 30)])["constraints"][0]
            assert v["status"] == STATUS_SAT, f"500ml 未命中 {name!r}"

    def test_one_meter_matches_1m(self):
        v = evaluate([_c("functional", "一米长")], [_item("a", "Braided USB-C Cable, 1m", 20)])["constraints"][0]
        assert v["status"] == STATUS_SAT

    def test_68_key_does_not_match_78_key(self):
        # 数字键位护栏：裸 key/keys 会误伤 78 键，必须带数字才是同一条要求。
        v = evaluate([_c("functional", "68键")], [_item("a", "Mechanical Keyboard 78 keys", 90)])["constraints"][0]
        assert v["status"] == STATUS_UNKNOWN

    def test_68_key_with_space_keeps_number(self):
        # 2026-11-13 实测：「68 键」（带空格）被 _keywords 的 \s 拆成 ["68","键"]，
        # 68 被 isdigit 过滤 → 关键词退化成裸「键」，文案显示「命中关键词「键」」。
        v = evaluate([_c("functional", "68 键")], [_item("a", "Mechanical Keyboard 68-key Compact", 90)])["constraints"][0]
        assert v["status"] == STATUS_SAT
        assert "68键" in v["evidence"], v["evidence"]

    def test_olive_green_hits_olive(self):
        # 2026-11-13 实测：商品描述写着 "Color/Fabric Type: Olive"，判定却 ❔ ——
        # 颜色词典只有「绿色→olive」，没有「橄榄绿」这个 key。
        v = evaluate(
            [_c("preference", "橄榄绿")],
            [_item("a", "Nylon Messenger Bag  Color/Fabric Type: Olive", 65.79)],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]

    def test_apparel_function_words_hit(self):
        cases = [
            ("防滑", "Non-Slip Rubber Outsole Running Shoes"),
            ("透气", "Breathable Mesh Upper Sneakers"),
            ("网面", "Engineered Mesh Running Shoes"),
            ("黑白拼色", "ANTA Running Shoes 'Black White'"),
            ("轻便", "Lightweight Crossbody Pouch"),
        ]
        for word, name in cases:
            v = evaluate([_c("functional", word)], [_item("a", name, 60)])["constraints"][0]
            assert v["status"] == STATUS_SAT, f"{word} 未能匹配 {name!r}：{v['reason']}"

    def test_lamp_function_words_hit(self):
        # 检索侧补了 台灯/护眼/全光谱/触控/调光，判定侧必须能核对同一批词。
        cases = [
            ("全光谱", "Full Spectrum LED Desk Lamp"),
            ("护眼", "Eye Care Flicker Free Desk Lamp"),
            ("触控", "Touch Control LED Desk Lamp"),
            ("调光", "Dimmable LED Desk Lamp"),
        ]
        for word, name in cases:
            v = evaluate([_c("functional", word)], [_item("a", name, 120)])["constraints"][0]
            assert v["status"] == STATUS_SAT, f"{word} 未能匹配 {name!r}：{v['reason']}"
class TestDescriptionIsEvidence:
    """2026-11-14 实测：Channel3 商品的颜色/材料/容量证据经常只出现在 description，
    name/category/tags 里没有 —— 旧版 _text_of 丢 description，结果「商品描述里明明
    写着 Olive / 10000 mAh / gunmetal 却判 ❔」。以下用例把 description 作为判定证据源钉死。
    """

    def _desc_item(self, name: str, description: str) -> dict:
        return {
            "id": f"d-{name}",
            "name": name,
            "category": "通用",
            "price": 60.0,
            "tags": [],
            "description": description,
        }

    def test_description_is_evidence_for_color(self):
        v = evaluate(
            [_c("preference", "橄榄绿")],
            [self._desc_item("Bygone Mini Crossbody", "Compact & Lightweight. Color/Fabric Type: Olive (olive green body, brown trim).")],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert v["rule"] == "attribute.color"

    def test_description_is_evidence_for_material(self):
        v = evaluate(
            [_c("preference", "尼龙")],
            [self._desc_item("Bygone Mini Crossbody", "Made from durable nylon shell with adjustable strap.")],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]

    def test_10000mah_matches_spaced_description(self):
        # 2026-11-14 实测：用户写「10000mAh」，商品描述写 "10000 mAh battery"（带空格）。
        # 旧版 _synonym_variants 只认 ml/毫升 → variants=('10000mah',) 匹配不上带空格 →
        # 充电宝案例 10000mAh 恒 ❔。扩到 mah/毫安时/wh/瓦后必须能对上。
        v = evaluate(
            [_c("functional", "10000mAh")],
            [self._desc_item("Power Bank Slim", "10000 mAh battery, 20W fast charging, USB-C.")],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "100" in v["evidence"], v["evidence"]

    def test_500ml_with_space_matches(self):
        v = evaluate(
            [_c("functional", "500ml")],
            [self._desc_item("Thermos Flask", "Stainless steel 500 ml vacuum insulated bottle.")],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]

    def test_deep_space_gray_color_key(self):
        # 2026-11-14 实测：充电宝「深空灰」❔ —— 词典只有基础色，没有「深空灰」key。
        v = evaluate(
            [_c("preference", "深空灰")],
            [self._desc_item("Power Bank Slim", "Gunmetal gray finish, 10000 mAh.")],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert v["rule"] == "attribute.color"

    def test_silver_gray_color_key(self):
        v = evaluate(
            [_c("preference", "银灰色")],
            [self._desc_item("LED Desk Lamp", "Silver gray aluminum body, touch dimmer.")],
        )["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
