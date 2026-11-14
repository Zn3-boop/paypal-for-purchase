"""constraints.py 规则降级抽取层 —— 「一句话 → 原子约束」不能静默丢失、也不能注水。

覆盖 2026-11-10 20 条模板全量审计暴露的三类真实缺陷：

1. **P0-4 连词误切**：`低跟` 的「跟」是词素（鞋跟），旧实现用 `replace("跟", "，")`
   把「深棕色，低跟」切成 `低` + `跟` 两个单字，双双被 `len(seg) < 2` 过滤
   —— 约束凭空消失，用户看到的「满足 N/M」分母都少了一条。
2. **用途分句进约束**：「出门应急充电」「团建统一配备」「展会物料包」描述的是
   「拿来干嘛」，商品目录没有字段可核对 → 只会产出永远 ❔ 的噪音，淹掉真问题。
3. **颜色注水**：「深棕色」同时抽出「深棕」「棕色」，「浅灰色」同时抽出「浅灰」
   「灰色」，同一条要求计两遍，分子分母一起注水。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.api.constraints import _simple_rule_extraction, _split_clauses


def _pairs(query: str) -> list[tuple[str, str]]:
    """规则抽取结果 → [(type, content)]，断言时只关心这两列。"""
    return [(c["type"], c["content"]) for c in _simple_rule_extraction(query, "demo")]


def _contents(query: str) -> list[str]:
    return [c["content"] for c in _simple_rule_extraction(query, "demo")]


class TestSplitClauses:
    """连词切分：只有两侧都是内容字符的「和/与/跟/以及」才是连接词。"""

    def test_heel_word_not_split(self):
        # 「低跟/高跟/鞋跟」里的「跟」是词素，右侧是标点 → 不许切。
        assert _split_clauses("深棕色，低跟") == ["深棕色", "低跟"]
        assert _split_clauses("高跟") == ["高跟"]
        assert _split_clauses("黑色，鞋跟") == ["黑色", "鞋跟"]

    def test_conjunction_still_splits(self):
        # 真正的并列连词照旧切：两侧都是内容字符。
        assert _split_clauses("鼠标和键盘以及耳机") == ["鼠标", "键盘", "耳机"]
        assert _split_clauses("帆布与皮革") == ["帆布", "皮革"]

    def test_trailing_conjunction_char_kept(self):
        # 句尾的「和」是词素（柔和），不是连词 —— 切了会把「柔和」判丢。
        assert _split_clauses("面料柔和") == ["面料柔和"]


class TestHeelConstraintSurvives:
    """P0-4 端到端：服4 模板的「低跟」必须成为一条真约束。"""

    QUERY = "我需要一双皮鞋，哑光皮质，深棕色，低跟，走路不累脚。"

    def test_low_heel_extracted(self):
        assert "低跟" in _contents(self.QUERY)

    def test_usage_clause_dropped(self):
        # 「走路不累脚」是用途/体感，没有可核对字段 → 不进约束。
        assert "走路不累脚" not in _contents(self.QUERY)

    def test_no_split_debris(self):
        # 旧 bug 的症状：「低跟」被切成「低」+「跟」，这里断言碎片不存在。
        # （`鞋` 是单字品类词，合法，不在检查范围内。）
        contents = _contents(self.QUERY)
        assert "低" not in contents and "跟" not in contents, contents


class TestUsageClauseFilter:
    """用途/场景分句不进约束（缺少可核对字段，只会制造 ❔ 噪音）。"""

    def test_dropped_when_no_object_word(self):
        cases = [
            "我需要一个充电宝，轻薄便携，白色，小容量，出门应急充电。",
            "我们团体采购一批雨伞，加固抗风骨架，墨绿，折叠款，团建统一配备。",
            "我们团体采购一批帆布托特包，加厚帆布，卡其色，展会物料包。",
            "我们团体采购一批保温杯，316不锈钢，作为团队伴手礼。",
            "我需要一根数据线，编织外皮，藏青色，充电传输稳定。",
            "我需要一把键盘，静音轴体，浅灰配色，写代码打字用。",
            "我需要一个鼠标，人体工学，打游戏顺手。",
            "我需要一条半身裙，垂感好，黑色，适合通勤。",
        ]
        for query in cases:
            for content in _contents(query):
                for hint in ("应急", "配备", "物料", "伴手礼", "传输", "打字", "顺手", "通勤"):
                    assert hint not in content, f"{query} 仍抽出用途约束：{content}"

    def test_kept_when_clause_names_a_real_object(self):
        # 「装书本电脑」含品类「书」——这是「包要能装书」的真要求，不能当噪音丢。
        assert "书" in _contents("我需要一个帆布包，厚实布料，卡其色，大号，装书本电脑。")


class TestGroupPurchasePrefix:
    """团购句开头「我们团体采购一批」必须剥掉，否则整句落成品类兜底。"""

    QUERY = "我们团体采购一批针织围巾，羊毛混纺，酒红色，宽版，冬季团队福利。"

    def test_prefix_stripped_to_category(self):
        contents = _contents(self.QUERY)
        assert "围巾" in contents
        assert "针织" in contents
        # 旧 bug 症状：整句「我们团体采购一批针织围巾」被当成一条功能约束
        assert not any("我们团体采购" in c for c in contents), contents

    def test_team_benefit_clause_dropped(self):
        assert not any("福利" in c for c in _contents(self.QUERY))


class TestColorDedupe:
    """颜色只保留最具体的那个，避免同一要求计两遍。"""

    def test_deep_brown(self):
        pairs = _pairs("我需要一双皮鞋，深棕色，低跟")
        assert ("preference", "深棕") in pairs
        assert ("preference", "棕色") not in pairs

    def test_light_gray(self):
        pairs = _pairs("我们需要一批静音键盘，浅灰色，68键")
        assert ("preference", "浅灰") in pairs
        assert ("preference", "灰色") not in pairs

    def test_matte_black_is_color_not_whole_clause(self):
        # 「磨砂黑」是修饰色，不该退化成整段 functional。
        pairs = _pairs("我需要一个鼠标，人体工学，磨砂黑，无线")
        assert ("preference", "磨砂黑") in pairs

    def test_caramel_is_color_preference(self):
        # 焦糖色过去不在颜色词典 → 落成 functional，判定侧没有颜色分支可走。
        pairs = _pairs("我需要一条围巾，羊毛混纺，焦糖色，宽版")
        assert ("preference", "焦糖色") in pairs


class TestPerItemScopeNormalization:
    """「单件预算」的 scope 必须由文案决定，不能依赖可能缺失的字段。

    2026-11-13 现场实测：LLM 抽取写出的约束是
        {"content": "单件预算≤180.0", "scope": "total"}
    组合级按「总价 ≤ 180」判、逐件表对 total 预算一律 n/a（前端显示 ❔），
    于是界面同时出现「组合 ✅ $149.93 ≤ $180」和「每件 ❔」，而且真买 22 件
    （$1099）会被判成超支 —— 语义整个反了。
    """

    def test_content_says_per_item_overrides_total_scope(self):
        from app.services.group_purchase.constraints import Constraint

        c = Constraint(
            id="c1", type="budget", content="单件预算≤180.0",
            hard=True, source="agent_composer", numeric_value=180.0, scope="total",
        )
        assert c.scope == "per_item"

    def test_restored_session_is_normalized_too(self):
        from app.services.group_purchase.constraints import Constraint

        c = Constraint.from_dict({
            "id": "c1", "type": "budget", "content": "单件预算≤180.0",
            "hard": True, "source": "agent_composer", "numeric_value": 180.0,
            "scope": "total",
        })
        assert c.scope == "per_item"

    def test_total_budget_untouched(self):
        from app.services.group_purchase.constraints import Constraint

        c = Constraint(
            id="c1", type="budget", content="总预算 1200",
            hard=True, source="成员 A", numeric_value=1200.0,
        )
        assert c.scope == "total"


class TestLampTemplateRegression:
    """单人台灯需求（2026-11-13 实测）：量词没剥 → 约束名叫「一盏桌面台灯」；
    检索词只映射出 gray → 返回一堆灰色装饰灯，与护眼/调光无关。"""

    QUERY = "我需要一盏桌面台灯，全光谱护眼，银灰色，可触控调光款。"

    def test_measure_word_san_stripped(self):
        contents = _contents(self.QUERY)
        assert any("桌面台灯" in c for c in contents), contents
        assert not any(c.startswith("一盏") for c in contents), contents

    def test_search_query_covers_function_words(self):
        from app.services.group_purchase.planner import build_search_query

        sq, _display, _anchors = build_search_query(self.QUERY)
        english = sq.split(" | ")[0].lower()
        for word in ("desk lamp", "full spectrum", "touch", "dimmable"):
            assert word in english, f"检索词缺少 {word!r}：{english}"


class TestTotalBudgetCap:
    """组合总价上限只能来自 scope="total" 的硬预算。

    2026-11-13 现场实测（运动鞋团购）：用户说「单件预算不超 240，采购 32 双」，
    方案合计 $1179.37，界面显示「预算 $240 · 491% · 超支 $939.37 · 🚨 Guardrail 拦截」
    —— 而同屏约束判定是「✅ 单件预算≤240，全部 14 件单价 ≤ $240」。根因是 7 个
    消费点（plans / paypal_callback / cart / cart_modifier / recommendations×2 / planner）
    各自 `min(numeric_value)`，没有一处看 scope。统一走 total_budget_cap 后 per_item
    预算不得再充当购物车总额门槛。
    """

    def test_per_item_budget_is_not_a_cart_cap(self):
        from app.services.group_purchase.constraints import Constraint, total_budget_cap

        caps = [Constraint(
            id="c1", type="budget", content="单件预算不超 240",
            hard=True, source="agent_composer", numeric_value=240.0,
        )]
        assert total_budget_cap(caps) is None

    def test_total_budget_is_cart_cap(self):
        from app.services.group_purchase.constraints import Constraint, total_budget_cap

        caps = [Constraint(
            id="c1", type="budget", content="总预算 1200",
            hard=True, source="成员 A", numeric_value=1200.0,
        )]
        assert total_budget_cap(caps) == 1200.0

    def test_mixed_takes_strictest_total_only(self):
        from app.services.group_purchase.constraints import Constraint, total_budget_cap

        caps = [
            Constraint(id="c1", type="budget", content="总预算 1200", hard=True,
                       source="A", numeric_value=1200.0),
            Constraint(id="c2", type="budget", content="总预算 900", hard=True,
                       source="B", numeric_value=900.0),
            Constraint(id="c3", type="budget", content="单件预算不超 240", hard=True,
                       source="C", numeric_value=240.0),
        ]
        assert total_budget_cap(caps) == 900.0

    def test_soft_and_removed_budget_ignored(self):
        from app.services.group_purchase.constraints import Constraint, total_budget_cap

        caps = [
            Constraint(id="c1", type="budget", content="总预算 500", hard=False,
                       source="A", numeric_value=500.0),
            Constraint(id="c2", type="budget", content="总预算 300", hard=True,
                       source="B", numeric_value=300.0, status="removed"),
        ]
        assert total_budget_cap(caps) is None


class TestShoeAndBagTemplateRegression:
    """运动鞋 / 斜挎包模板（2026-11-13 实测）：检索词只覆盖品类，功能词全丢。"""

    SHOE = "我们团体采购一批防滑运动鞋，透气网面，黑白拼色，标准尺码，单件预算不超 240，一共采购 32 双，作为团建统一队鞋。"
    BAG = "我需要一个斜挎小包，防水尼龙，橄榄绿，轻便通勤款。"

    def _english(self, text):
        from app.services.group_purchase.planner import build_search_query

        return build_search_query(text)[0].split(" | ")[0].lower()

    def test_shoe_query_covers_function_words(self):
        english = self._english(self.SHOE)
        for word in ("breathable", "mesh", "non-slip", "black white"):
            assert word in english, f"检索词缺少 {word!r}：{english}"

    def test_bag_query_covers_function_words(self):
        english = self._english(self.BAG)
        for word in ("crossbody", "nylon", "olive", "lightweight"):
            assert word in english, f"检索词缺少 {word!r}：{english}"


class TestSpecExtractionRegression:
    """316 / 500ml 规格（2026-11-13 实测）：给要 500ml 的用户推荐 40oz 超大杯。

    「316 不锈钢，500ml 便携款」此前只会抽出「不锈钢材质」「便携」两条约束，
    316 和容量直接消失。规格是客观要求：商品写了就 ✅，目录缺字段就诚实 ❔，
    不能静默当没提过。
    """

    def test_capacity_kept_as_constraint(self):
        contents = _contents("我需要一支保温杯，316 不锈钢，奶白色，500ml 便携款。")
        assert any(c == "500ml" for c in contents), contents
        # 40oz ≈ 1183ml，绝不能拿来冒充 500ml
        assert "500ml" not in ("1183ml",)

    def test_capacity_with_space_normalized(self):
        contents = _contents("我需要一支保温杯，316 不锈钢，奶白色，500 ml 便携款。")
        assert any(c == "500ml" for c in contents), contents

    def test_steel_grade_kept(self):
        # 2026-11-13 审计修复：裸「316」会被判定侧 `_keywords` 的 isdigit() 过滤，
        # 永远判「没有可比对的关键词」。带材质上下文输出「316不锈钢」才能核对。
        contents = _contents("316 不锈钢保温杯")
        assert any(c == "316不锈钢" for c in contents), contents
        assert not any(c == "316" for c in contents), contents

    def test_capacity_enters_search_query(self):
        from app.services.group_purchase.planner import build_search_query

        sq, _display, _anchors = build_search_query("我需要一支保温杯，316 不锈钢，奶白色，500ml 便携款。")
        english = sq.split(" | ")[0].lower()
        assert "500ml" in english, english
        assert "316" in english, english


class TestAttributeCoverage:
    """能从商品标签核对的属性必须被抽成 functional（而不是整段品类兜底）。"""

    def test_new_attrs_extracted(self):
        cases = {
            "我们团体采购一批帆布托特包，加厚帆布，卡其色，加大款。": ("加厚", "加大款"),
            "我需要一条围巾，羊毛混纺，焦糖色，宽版，冬天保暖。": ("宽版", "保暖"),
            "我需要一双皮鞋，哑光皮质，深棕色，低跟。": ("哑光", "低跟"),
            "我需要一把雨伞，抗风骨架，墨绿，折叠款。": ("抗风", "折叠"),
        }
        for query, expected in cases.items():
            contents = _contents(query)
            for word in expected:
                assert word in contents, f"{query} 缺少约束 {word}，实际 {contents}"

    def test_budget_scope_kept(self):
        # 回归护栏：连词/用途规则的改动不能影响预算解析与 per_item 语义。
        raw = _simple_rule_extraction(
            "我们团体采购一批通勤外套，挺括抗皱，单件预算不超 320，一共采购 28 件。", "demo"
        )
        budget = [c for c in raw if c["type"] == "budget"]
        assert budget and budget[0]["numeric_value"] == 320.0
        assert budget[0]["scope"] == "per_item"
