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
from app.services.group_purchase.satisfaction import STATUS_SAT, evaluate  # noqa: E402


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