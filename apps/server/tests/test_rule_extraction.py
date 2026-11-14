"""规则降级抽取：整句 → 原子约束（LLM 不可用时的最后防线）。

真实背景（2026-11-10 实测）：LLM 端点 403、Channel3 超时，系统降级到规则抽取。
旧实现把整句当一条约束，结果只有两种极端：
  - 「我需要一件大衣，有设计感，呢绒的，红色，中长款」→ 一个关键词 → 目录 0 命中 → 无解；
  - 「买一副耳机，听音乐，音质好的，小巧」→ 一条都没抽到 → 不过滤 → 方案是塑料餐具。
这里锁死的是：每个用户要求都能变成可逐条核对的原子约束。
"""
from __future__ import annotations

from app.api.constraints import _simple_rule_extraction as extract


def contents(items: list[dict]) -> list[str]:
    return [c["content"] for c in items]


def by_content(items: list[dict]) -> dict[str, dict]:
    return {c["content"]: c for c in items}


def test_coat_brief_splits_into_atomic_constraints():
    """大衣需求：品类硬约束 + 材质/颜色/款式/主观各一条，不再是一整句。"""
    text = "我需要一件大衣，有设计感，呢绒的，红色，中长款"
    got = extract(text, "user")
    contents_ = contents(got)

    assert "大衣" in contents_
    assert "呢绒材质" in contents_
    assert "红色" in contents_
    assert "中长款" in contents_
    assert "设计感" in contents_
    # 整句不能再作为一条约束（它谁也匹配不上）
    assert text not in contents_
    # 品类是硬约束：用户要大衣，给餐具就是错
    assert by_content(got)["大衣"]["hard"] is True
    assert by_content(got)["大衣"]["type"] == "functional"
    # 材质/颜色/款式/主观都是软偏好：目录缺字段时判 ❔，不冤枉 ❌
    for soft in ("呢绒材质", "红色", "中长款", "设计感"):
        assert by_content(got)[soft]["hard"] is False


def test_earphone_brief_yields_category_and_drops_use_case():
    """耳机需求：旧实现抽 0 条 → 不过滤 → 方案全是餐具。"""
    got = extract("买一副耳机，听音乐，音质好的，小巧", "user")
    contents_ = contents(got)

    assert "耳机" in contents_       # 量词「一副」已剥掉
    assert "副耳机" not in contents_
    assert "小巧" in contents_
    assert "音质好" in contents_
    assert "听音乐" not in contents_  # 用途不是约束
    assert by_content(got)["耳机"]["hard"] is True
    assert by_content(got)["小巧"]["hard"] is True
    assert by_content(got)["音质好"]["type"] == "preference"  # 无评分字段 → 永远 ❔


def test_negated_clause_is_not_turned_into_positive_material():
    """「不要塑料」不能被抽成正面的「塑料材质」——语义会直接反掉。"""
    got = extract("帮我买露营用品，不要塑料", "user")
    assert "塑料材质" not in contents(got)
    assert any(c["type"] == "taboo" for c in got)


def test_compound_noun_converges_to_searchable_category():
    """「环保桌游奖品」→ 环保(软) + 桌游(硬)；整段当关键词是匹配不上的。"""
    got = extract("帮我找 5 人份的环保桌游奖品，预算 300 美元，不要塑料", "user")
    contents_ = contents(got)

    assert any(c["type"] == "budget" and c["numeric_value"] == 300.0 for c in got)
    assert "环保" in contents_
    assert "桌游" in contents_
    assert "环保桌游奖品" not in contents_


def test_substring_duplicates_are_collapsed():
    """「中长款」已表达「长款」，「音质好」已表达「音质」，不该各记一条。"""
    got = extract("红色呢绒中长款大衣", "user")
    contents_ = contents(got)
    assert "中长款" in contents_
    assert "长款" not in contents_

    got2 = extract("音质好的耳机", "user")
    contents2 = contents(got2)
    assert "音质好" in contents2
    assert "音质" not in contents2
