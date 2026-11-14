"""Channel3 字段抽取回归（2026-11-14）：materials / structured_attributes 是判定层的关键证据。

根因（真实网络审计复现）：用户三场景 UI 里「材质/颜色/数字/版型」判 ❔，
不是判定词典缺词、也不是 Channel3 没数据 —— 是 `_normalize_product` 只抓
key_features/brands 进 tags，把 Product.materials（['stainless steel']）与
Product.structured_attributes（{'outerwear-length-type': ['Hip length'],
'drinkware-material': ['Stainless steel'], ...}）整个丢掉。判定层 _text_of
（name+category+tags+description）根本看不到这些证据，于是「查得到却证明不了」。

本组用例钉死三件事：
  1. _normalize_product 必须把 materials / structured_attributes 的值并入 tags（去重）；
  2. 归一化后的商品（真实字段）走 evaluate，材质/颜色/版型/数字约束能翻 ✅ 并带证据；
  3. 版型词典补丁（hip length / crop / knee-length）支撑真实 structured_attributes 词形。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")

from app.services.channel3.client import _normalize_product  # noqa: E402
from app.services.group_purchase.constraints import Constraint  # noqa: E402
from app.services.group_purchase.satisfaction import STATUS_SAT, STATUS_UNSAT, evaluate  # noqa: E402


def _product(**kw) -> SimpleNamespace:
    """轻量 Product 替身：_normalize_product 只 getattr，pydantic 必填字段不必要。"""
    base = {
        "id": "ch3_t1",
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
    return SimpleNamespace(url="https://x", domain="x.com",
                           price=SimpleNamespace(price=price, currency=currency))


def _item_kwargs(name: str, price: float, desc: str, materials, attrs, features=None):
    return {
        "id": f"ch3_{abs(hash(name)) % 100000}",
        "title": name,
        "description": desc,
        "category": SimpleNamespace(title="Coats & Jackets", slug="coats-jackets"),
        "offers": [_offer(price)],
        "key_features": features,
        "materials": materials,
        "structured_attributes": attrs,
        "images": [SimpleNamespace(url="https://img/x.jpg")],
    }


def _c(idx: int, ctype: str, content: str, num=None, scope="total") -> Constraint:
    return Constraint(id=f"c{idx}", type=ctype, content=content, hard=True,
                      source="demo", numeric_value=num, scope=scope)


# ── 一档：归一化层不丢字段 ──────────────────────────────────────────
class TestNormalizeProductKeepsStructuredEvidence:
    def test_materials_and_structured_attributes_merged_into_tags(self):
        p = _product(**_item_kwargs(
            name="Thermos Jar", price=20.0,
            desc="A 500ml insulated jar.",
            materials=["stainless steel", "Tritan"],
            attrs={"drinkware-material": ["stainless steel", "stainless steel"],
                   "outerwear-length-type": ["Hip length"]},
        ))
        out = _normalize_product(p)
        tags = [t.lower() for t in out["tags"]]
        assert "stainless steel" in tags          # materials 并入（去重后仍在）
        assert "hip length" in tags               # structured_attributes 值并入
        assert "tritan" in tags
        assert tags.count("stainless steel") == 1  # 跨字段去重

    def test_no_structured_fields_is_still_ok(self):
        p = _product(id="ch3_t2", title="Plain Mug", offers=[_offer(5.0)],
                     key_features=["Mug"], materials=None, structured_attributes=None,
                     category=SimpleNamespace(title="Mugs", slug="mugs"))
        out = _normalize_product(p)
        assert out["tags"] == ["Mug"]
        assert "materials" not in out and "structured_attributes" not in out
# ── 二档：真实字段 → evaluate 翻 ✅（材质/颜色/版型/数字）────────────
class TestRealFieldsTurnUnknownIntoSat:
    def test_navy_jacket_navy_structured_and_hip_length_all_sat(self):
        """真实审计：navy 外套 description 含 'navy blue'/'structured shoulders'，
        structured_attributes['outerwear-length-type']=['Hip length']。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Jacket", price=220.0,
            desc="This navy blue stretch-jersey jacket features a tailored silhouette with structured shoulders.",
            materials=["polyester", "viscose", "elastane", "cotton"],
            attrs={"outerwear-length-type": ["Hip length"], "fabric": ["Polyester", "Cotton"]},
        )))
        for content in ("藏青", "挺括", "中长款"):
            v = evaluate([_c(1, "functional", content)], [item])["constraints"][0]
            assert v["status"] == STATUS_SAT, f"{content}: {v['reason']}"
            assert v["evidence"], f"{content} 应有证据"

    def test_cardigan_crop_top_is_short_version_sat(self):
        """真实审计：Luxe Knit Cardigan structured_attributes['top-length-type']=['Crop top']。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Luxe Knit Cardigan", price=29.99,
            desc="A soft knit cardigan for layering.",
            materials=["Polyester", "Wool"],
            attrs={"top-length-type": ["Crop top"], "fabric": ["Polyester", "Wool"]},
        )))
        v = evaluate([_c(1, "functional", "短款")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "crop" in v["evidence"].lower()

    def test_stainless_jar_material_and_500ml_sat(self):
        """真实审计：Beaba Jar materials=['stainless steel']，description 含 '500ml'。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Beaba Stainless Steel Insulated Jar", price=35.0,
            desc="The Beaba Stainless Steel Insulated Jar is a 500ml (16.9 fl. oz.) container.",
            materials=["stainless steel"],
            attrs={"drinkware-material": ["Stainless steel"],
                   "drinkware-insulation": ["Vacuum insulated"]},
        )))
        for content in ("316不锈钢", "不锈钢材质"):
            v = evaluate([_c(1, "functional", content)], [item])["constraints"][0]
            assert v["status"] == STATUS_SAT, f"{content}: {v['reason']}"
            assert "stainless" in v["evidence"].lower()
        v = evaluate([_c(2, "functional", "500ml")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "500" in v["evidence"]

    def test_insulated_tumbler_white_from_description_sat(self):
        """真实审计：Ashland 40oz Tumbler description 含 'in white'，materials 含 stainless。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="40oz. Stainless Steel Insulated Tumbler", price=9.99,
            desc="A 40oz stainless steel insulated tumbler by Ashland in white.",
            materials=["stainless steel", "plastic"],
            attrs={"drinkware-material": ["Stainless steel"],
                   "drinkware-insulation": ["Vacuum insulated"]},
        )))
        v = evaluate([_c(1, "functional", "白色")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "white" in v["evidence"].lower()

    def test_jia_da_short_key_matches_large(self):
        """2026-11-14 实测定死：「加大款」被抽取层切短成「加大」时，查的是 STYLE_KEYWORDS['加大']
        的变体 —— 旧版只有 (oversized/extra large/jumbo/xl) 缺 `large`，商品 key_features 明明写
        'Large capacity' 却判 ❔（「加大款」key 有 large 而「加大」没有，口语走哪个全看抽取层怎么切）。
        补上 `large` 后，content=加大 必须命中 Large capacity。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Canvas Tote Bag", price=2.99,
            desc="A large canvas tote bag for everyday use.",
            materials=["canvas"],
            attrs={"pattern": ["Solid"]},
        )))
        v = evaluate([_c(1, "functional", "加大")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "large" in v["evidence"].lower()


# ── 三档：诚实红线不破 ──────────────────────────────────────────────
class TestHonestyBoundary:
    def test_absent_evidence_stays_unknown(self):
        """没有证据仍 ❔：图像 colors(hex) 不进文本，描述里没有的颜色词不硬判。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Plain Cardigan", price=12.0,
            desc="V-neck / Puff sleeves / Cardigan with button closure",
            materials=["100% Polyester"],
            attrs={"pattern": ["Solid"]},
        )))
        v = evaluate([_c(1, "functional", "米白色")], [item])["constraints"][0]
        assert v["status"] != STATUS_SAT, "无颜色证据时不可硬判 ✅"


# ── 四档：皮鞋场景词表补全 + 哑光反证（2026-11-14 实测）──────────────
_LOW_PUCKER = dict(
    name="LOW PUCKER PUMP 45", price=1400.0,
    desc="Shiny oxblood leather finish / Rounded toe / Elasticated topline",
    materials=["leather"],
    attrs={},
)


class TestShoeVocabularyAndMatteCounterEvidence:
    def test_shoe_constraint_matches_pump(self):
        """真实商品 LOW PUCKER PUMP 45 名称只写 PUMP 不写 shoes —— 「鞋」变体现含 pump，
        不再 0/4（修前整批候选 ❔ 的根因之一）。"""
        item = _normalize_product(_product(**_item_kwargs(**_LOW_PUCKER)))
        v = evaluate([_c(1, "functional", "鞋")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "pump" in v["evidence"].lower()

    def test_leather_shoe_constraint_matches_leather_finish(self):
        """约束=皮鞋（整词）：商品 desc 写 'leather finish' → 命中，不再只能靠裸 shoes。"""
        item = _normalize_product(_product(**_item_kwargs(**_LOW_PUCKER)))
        v = evaluate([_c(1, "functional", "皮鞋")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "leather finish" in v["evidence"].lower()

    def test_matte_contradicted_by_shiny(self):
        """约束=哑光，商品明确写 Shiny → 确证违反（❌），而不是「无法判定」。
        商品自己说了是亮面，这比 ❔ 更诚实。"""
        item = _normalize_product(_product(**_item_kwargs(**_LOW_PUCKER)))
        v = evaluate([_c(1, "functional", "哑光")], [item])["constraints"][0]
        assert v["status"] == "unsat", v["reason"]
        assert "shiny" in v["evidence"].lower()

    def test_matte_without_counter_evidence_stays_unknown(self):
        """约束=哑光，商品既无 matte 也无 shiny → 仍是 ❔（不硬判）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Plain Mug", price=5.0, desc="Simple ceramic mug",
            materials=["ceramic"], attrs={"pattern": ["Solid"]},
        )))
        v = evaluate([_c(1, "functional", "哑光")], [item])["constraints"][0]
        assert v["status"] == "unknown", v["reason"]

    def test_shoe_constraint_still_matches_classic_words(self):
        """回归：扩充后的「鞋」词表不丢原来的 shoes/sneakers/boots。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Running Sneakers", price=30.0, desc="Comfortable sneakers for daily wear",
            materials=["canvas"], attrs={},
        )))
        v = evaluate([_c(1, "functional", "鞋")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]

    def test_low_heel_matches_low_heel_shoes(self):
        """真实商品 'Chic Mary Jane Low Heel Shoes' → 低跟 ✅（low heel 词条原样可用）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Chic Mary Jane Low Heel Shoes", price=39.99,
            desc="Vintage-inspired flats with a low heel.",
            materials=["Patent Leather", "Synthetic"], attrs={},
        )))
        v = evaluate([_c(1, "functional", "低跟")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "low heel" in v["evidence"].lower()


# ── 五档：容量数值语义 + 快充词表（2026-11-14 实测新场景）──────────────
class TestCapacityNumericSemantics:
    def test_cup_40oz_geq_500ml_with_conversion(self):
        """保温杯：标题 40oz ≈ 1183ml ≥ 500ml → ✅ 带换算证据（不再因文本无 '500ml' 判 ❔）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Wellness 40oz. Double Wall Vine Stripe Travel Cup", price=14.99,
            desc="Double-wall insulated travel cup, spill-resistant lid.",
            materials=["stainless steel"], attrs={},
        )))
        v = evaluate([_c(1, "functional", "500ml")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "40oz" in v["evidence"], v["evidence"]
        assert "1183" in v["evidence"], v["evidence"]

    def test_power_bank_headline_capacity_is_the_basis(self):
        """充电宝 10000mAh：判定依据标题主容量 30000mAh（≥ 10000mAh），
        而不是 key_features 里'本系列也有 10000mAh'的家族描述（修前假阳性）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="30000mAh Fast Charging Power Bank PD18W Portable Charger", price=74.09,
            desc="SUPER FAST CHARGING: 10000mAh supports PD18W fast charging, "
                 "while 20000mAh/30000mAh support PD22.5W.",
            materials=["plastic"], attrs={},
            features=["10000mAh supports PD18W fast charging"],
        )))
        v = evaluate([_c(1, "functional", "10000mAh")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "30000mAh" in v["evidence"] and "10000mAh" in v["evidence"], v["evidence"]
        assert "主容量" in v["evidence"]
        assert "10000mAh supports PD18W" not in v["evidence"], "不允许用家族描述当证据"

    def test_power_bank_below_wanted_is_confirmed_unsat(self):
        """标题主容量 2000mAh < 需求 10000mAh → ❌（商品自己声明了容量，确证不满足）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="2000mAh Slim Pocket Power Bank", price=9.9,
            desc="Great for topping up earbuds between charges.",
            materials=["plastic"], attrs={},
        )))
        v = evaluate([_c(1, "functional", "10000mAh")], [item])["constraints"][0]
        assert v["status"] == STATUS_UNSAT, v["reason"]
        assert "2000mAh" in v["evidence"] and "10000mAh" in v["evidence"], v["evidence"]

    def test_power_bank_unsat_not_contradicted_by_item_row(self):
        """组合层 ❌ 时逐件表不得因子串命中标 ✅（同一套容量语义，杜绝自相矛盾）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="2000mAh Slim Pocket Power Bank", price=9.9,
            desc="Great for topping up earbuds between charges.",
            materials=["plastic"], attrs={},
        )))
        out = evaluate([_c(1, "functional", "10000mAh")], [item])
        assert out["constraints"][0]["status"] == STATUS_UNSAT
        row = out["items"][0]["results"][0]
        assert row["status"] == "fail", row

    def test_fast_charging_vocabulary_matches_fast_charging(self):
        """「快充」：商品标题 Fast Charging → ✅（修前词表只有 充电/charger，快充恒 ❔）。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="30000mAh Fast Charging Power Bank PD18W Portable Charger", price=74.09,
            desc="SUPER FAST CHARGING: 10000mAh supports PD18W fast charging.",
            materials=["plastic"], attrs={},
        )))
        v = evaluate([_c(1, "functional", "快充")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "fast charging" in v["evidence"].lower(), v["evidence"]

    def test_no_headline_capacity_falls_back_to_keyword_path(self):
        """标题无容量时回到关键词路径：desc 里 'A 500ml insulated jar' 仍是可判定证据。"""
        item = _normalize_product(_product(**_item_kwargs(
            name="Thermos Jar", price=20.0, desc="A 500ml insulated jar.",
            materials=["stainless steel"], attrs={},
        )))
        v = evaluate([_c(1, "functional", "500ml")], [item])["constraints"][0]
        assert v["status"] == STATUS_SAT, v["reason"]
        assert "500ml" in v["evidence"].lower()