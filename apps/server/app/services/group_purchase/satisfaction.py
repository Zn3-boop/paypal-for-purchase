"""约束满足度判定 —— 让「✅/❔/❌」有证据、可复算、可追溯。

设计红线（本项目已定死）：
  ✅ 必须**可判定 + 带证据 + 可追溯**；否则它只是装饰，比没有更糟。
  ❌ 只有「确证违反」才判 unsat：数字超支、禁忌词真的出现。
  ❔ 缺证据 ≠ 违反。「没找到匹配关键词」也可能是目录没写全/商品描述缺字段，
     因此未命中一律 `unknown` 并说明缺了什么 —— 绝不假装 ✅，也不冤枉 ❌。

判定档位（诚实分级）：
  确证满足 sat  ：预算未超支（数字）/ 功能词命中 / 环保标签命中 / 材质词命中 / 未出现禁忌词
  确证违反 unsat：预算超支 / 禁忌词出现
  无法判定 unknown：主观质量 / 未命中任何关键词（功能、材质、环保）/ 无结构化字段

每条判定都带 `rule`：命中的是哪条规则（规则表 RULES，自解释、可复算）。
预算与“共性缺失理由”永远挂在**组合层**（scope="combo"），不逐件复制。

本模块是**纯函数**：不读会话、不写审计、不改 schema。输入约束 + 商品，输出判定。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.services.group_purchase.text_units import split_on_conjunctions

logger = logging.getLogger(__name__)

STATUS_SAT = "sat"
STATUS_UNSAT = "unsat"
STATUS_UNKNOWN = "unknown"

# ── 规则表：约束类型 → 判定规则名 ─────────────────────────────────
# 每条判定都携带命中的 rule，前端可直接展示"按哪条规则判的"，也方便审计复算。
RULES: dict[str, str] = {
    "budget": "budget.balance",              # 数字比较：组合总价 vs 预算上限
    "functional": "functional.keyword_any",  # 任一商品 名称/品类/标签 命中关键词
    "taboo": "taboo.forbidden_present",      # 任一商品命中禁忌词 → 确证违反
    "material": "material.keyword_any",      # 材质词典命中（K1 目录可核对）
    "eco": "eco.label_evidence",             # 环保标签证据词命中
    "preference": "preference.unverifiable", # 主观质量 → unknown
}
# 未命中时统一挂的组合层规则：证据缺失 ≠ 违反
RULE_NO_EVIDENCE = "common.missing_evidence"

# 环保语义：命中任一即认为该约束是"可判的环保约束"
ECO_CONTENT_HINTS = ("环保", "eco", "绿色", "可持续", "sustainable", "recycled", "再生", "有机", "organic")
# 商品侧环保证据词：只收「明确环保主张」，不收 cotton/帆布 这类中性材质 ——
# 否则「普通棉T恤」会被判成 ✅（那就是撒谎）。
ECO_ITEM_EVIDENCE = (
    "环保", "eco", "recycled", "再生", "有机", "organic", "bamboo", "竹",
    "可降解", "sustainable", "rpet", "fsc",
)
# 材质词 → 商品侧可核对的证据词（K1 catalog: 不锈钢/塑料/帆布/木质/铝）
MATERIAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "不锈钢": ("不锈钢", "stainless"),
    "塑料": ("塑料", "plastic", "pp", "abs"),
    "帆布": ("帆布", "canvas"),
    "木质": ("木质", "木", "wood", "bamboo"),
    "铝": ("铝", "aluminum", "aluminium"),
    "玻璃": ("玻璃", "glass"),
    "纸": ("纸", "paper", "再生纸"),
    # 纺织类：用户口语句式的常见材质（「呢绒的」「羊毛的」），
    # Channel3 真实商品描述里会写 wool / tweed / leather，所以中英都收。
    "呢绒": ("呢绒", "羊毛", "羊绒", "wool", "tweed", "cashmere"),
    # 「羊毛」单独成 key（2026-11-10 审计）：抽取层把「羊毛混纺」抽成 content「羊毛材质」，
    # 这里只有「呢绒」key 时 `呢绒 in 羊毛材质` 为 False → 走不到材质分支 → 永远 ❔。
    "羊毛": ("羊毛", "wool", "merino", "lambswool", "wool blend"),
    "皮质": ("皮质", "皮革", "真皮", "leather", "suede"),
    "羽绒": ("羽绒", "down"),
    "皮革": ("皮革", "真皮", "leather"),
    "真丝": ("真丝", "丝绸", "silk"),
    "亚麻": ("亚麻", "linen"),
    "牛仔": ("牛仔", "denim"),
    "棉": ("棉", "cotton"),
    # 2026-11-14 实测：材料用例「防水尼龙斜挎包」的「尼龙」、「加厚陶瓷马克杯」的「陶瓷」、
    # 「抗风玻纤骨架雨伞」的「玻纤」三连 ❔ —— 词表漏了这三种常见材料。注意材料分支
    # （_judge_constraint）只认 MATERIAL_KEYWORDS，SYNONYM_KEYWORDS 里有 ceramic 也进不来。
    "尼龙": ("尼龙", "nylon", "cordura", "polyamide", "ballistic"),
    "陶瓷": ("陶瓷", "ceramic", "porcelain", "stoneware", "earthenware"),
    "玻纤": ("玻纤", "fiberglass", "fibreglass", "glass fibre", "glass fiber"),
}
# 用户口语 → 商品侧证据词（中英同义）。只收**客观属性**：
# 品类（耳机=headphones/earbuds）、形态（小巧=compact/portable/foldable）、
# 版型（中长款=longline/midi）。
# 主观质量（音质好、有设计感）仍走 SUBJECTIVE_HINTS → ❔，绝不翻译成 ✅。
# 为什么需要（2026-11-10 实测）：Channel3 是英文商品库，中文约束「耳机」对
# "T110 In-Ear Headphones" 一个字符都匹配不上，硬判定只能全 ❔ —— 那是
# "我们没查" 而不是 "查不到"，同样不诚实。
SYNONYM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "耳机": ("耳机", "headphones", "headphone", "earbuds", "earphones", "headset", "in-ear", "on-ear", "earphone"),
    "音箱": ("音箱", "speaker"),
    "大衣": ("大衣", "coat", "overcoat", "trench"),
    "外套": ("外套", "jacket", "coat"),
    "卫衣": ("卫衣", "hoodie", "sweatshirt"),
    "衬衫": ("衬衫", "shirt", "blouse"),
    "裙": ("裙", "skirt", "dress"),
    "鞋": ("鞋", "shoes", "sneakers", "boots"),
    "水杯": ("水杯", "water bottle", "bottle", "tumbler", "flask"),
    "背包": ("背包", "backpack", "rucksack"),
    "台灯": ("台灯", "desk lamp", "table lamp"),
    "键盘": ("键盘", "keyboard", "keyboard case", "mechanical keyboard"),
    "鼠标": ("鼠标", "mouse"),
    "静音": ("静音", "silent", "quiet", "low profile", "noise reducing"),
    "按键": ("按键", "key switch", "keycap", "button"),
    "顺滑": ("顺滑", "smooth", "silky"),
    "牛仔裤": ("牛仔裤", "jeans", "denim pants", "denim"),
    "高腰": ("高腰", "high rise", "high-rise", "high waist", "high-waist"),
    "水洗蓝": ("水洗蓝", "washed blue", "light blue denim"),
    "水洗": ("水洗", "washed", "wash", "stone wash", "stonewashed"),
    "直筒": ("直筒", "straight leg", "straight cut", "straight fit"),
    "桌游": ("桌游", "board game", "boardgame", "tabletop game", "table top game"),
    "掉色": ("掉色", "colorfast", "fade-resistant", "non-fading"),
    "围巾": ("围巾", "scarf", "scarves", "shawl", "wrap"),
    "羊毛": ("羊毛", "wool", "merino", "lambswool"),
    "焦糖色": ("焦糖色", "caramel", "tan", "toffee"),
    "宽版": ("宽版", "wide", "oversized"),
    "保暖": ("保暖", "warm", "thermal", "insulated"),
    "马克杯": ("马克杯", "mug", "coffee mug", "ceramic mug", "teacup"),
    "陶瓷": ("陶瓷", "ceramic", "porcelain", "stoneware", "earthenware"),
    "奶白": ("奶白", "cream", "off-white", "ivory", "milky white"),
    "小巧": ("小巧", "compact", "portable", "mini", "foldable"),
    "便携": ("便携", "portable", "compact", "foldable"),
    "降噪": ("降噪", "noise cancelling", "noise-cancelling", "noise canceling", "anc"),
    "无线": ("无线", "wireless", "bluetooth"),
    "有线": ("有线", "wired"),
    "充电宝": ("充电宝", "power bank", "powerbank", "portable charger", "battery bank", "external battery", "usb charger", "booster"),
    "移动电源": ("移动电源", "power bank", "powerbank", "portable charger", "battery bank", "external battery"),
    "充电器": ("充电器", "charger", "charging", "power adapter"),
    "便携充电": ("便携充电", "portable charger", "power bank"),
    "袋子": ("袋子", "bag", "tote", "pouch", "duffel"),
    "帆布包": ("帆布包", "canvas bag", "canvas tote", "canvas backpack", "canvas pouch"),
    "厚实": ("厚实", "rugged", "thick", "heavyweight", "heavy-duty", "substantial", "sturdy", "durable"),
    "大号": ("大号", "large", "big", "roomy", "spacious", "oversized", "capacity"),
    "书": ("书", "book", "books", "laptop"),
    "书本": ("书本", "book", "books", "laptop"),
    # ── 20 条模板需求（2026-11-10 全量审计）补齐：中文约束词 → 英文商品证据词 ──
    # 服饰
    "半身裙": ("半身裙", "skirt", "midi skirt", "a-line skirt"),
    "开衫": ("开衫", "cardigan", "knit cardigan"),
    "针织": ("针织", "knit", "knitted", "knitwear", "pointelle"),
    "垂感": ("垂感", "drape", "draped", "flowy", "fluid", "soft drape"),
    "软糯": ("软糯", "soft", "cozy", "plush", "gentle"),
    "亲肤": ("亲肤", "skin-friendly", "soft", "soft touch", "gentle", "breathable"),
    "哑光": ("哑光", "matte", "matt", "matte finish", "suede", "brushed"),
    "低跟": ("低跟", "low heel", "low-heel", "flat heel"),
    "挺括": ("挺括", "structured", "crisp", "firm"),
    "抗皱": ("抗皱", "wrinkle-resistant", "wrinkle resistant", "wrinkle free", "non-iron"),
    # 数码
    # 「小容量」的商品侧写法：小容量充电宝基本都卖「compact / slim / mini」，
    # 商品描述很少写 capacity 具体形容词 —— 只收 "compact capacity" 会永远落空。
    "小容量": ("小容量", "compact", "compact capacity", "small capacity", "mini", "slim"),
    "人体工学": ("人体工学", "ergonomic", "ergonomically"),
    "数据线": ("数据线", "cable", "charging cable", "usb cable", "usb-c", "lightning cable", "cord"),
    "编织": ("编织", "braided", "woven", "nylon braided", "braided cable"),
    # 生活
    "雨伞": ("雨伞", "umbrella", "parasol"),
    "抗风": ("抗风", "windproof", "wind-resistant", "wind resistant", "vented"),
    "加固": ("加固", "reinforced", "sturdy", "heavy duty"),
    "折叠": ("折叠", "foldable", "folding", "collapsible", "compact"),
    "宽口": ("宽口", "wide mouth", "wide rim", "wide opening"),
    "一米": ("一米", "1m", "1 m", "3ft", "39in", "100cm"),
    "保温": ("保温", "insulated", "thermal", "vacuum insulated", "keeps warm"),
    # 团购
    "耐磨": ("耐磨", "durable", "abrasion resistant", "wear-resistant", "rubberized"),
    "防水": ("防水", "waterproof", "water-resistant", "water resistant", "water repellent"),
    "蓝牙": ("蓝牙", "bluetooth", "bt"),
    "加厚": ("加厚", "thick", "heavyweight", "heavy duty", "double layered"),
    "加大": ("加大款", "oversized", "extra large", "jumbo", "xl", "large"),
    "托特包": ("托特包", "tote", "tote bag", "shopper bag"),
    "保温杯": ("保温杯", "insulated cup", "tumbler", "travel mug"),
    # 台灯（2026-11-13 实测：检索侧补了功能词，判定侧也必须能核对，
    # 否则又是「查到了却证明不了」）
    "护眼": ("护眼", "eye care", "eye-care", "eye protection", "flicker free", "flicker-free", "anti-glare"),
    "全光谱": ("全光谱", "full spectrum", "full-spectrum", "cri 95", "cri95", "ra95"),
    "触控": ("触控", "touch", "touch control", "touch-control", "touch sensor", "touch-sensitive"),
    "调光": ("调光", "dimmable", "dimmer", "dimming"),
}

# 颜色：偏好里的**客观属性**。命中即 ✅（带证据），未命中 → ❔（描述可能没写颜色）。
# 中文词 + 英文同义词都收，英文用词边界匹配（`red` 不能命中 `colored`）。
COLOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "红色": ("红色", "red", "burgundy", "crimson", "maroon", "scarlet", "wine"),
    "蓝色": ("蓝色", "blue", "navy", "cobalt", "indigo"),
    "黑色": ("黑色", "black", "noir"),
    "白色": ("白色", "white", "ivory", "off-white"),
    "绿色": ("绿色", "green", "olive", "sage", "khaki"),
    "灰色": ("灰色", "grey", "gray", "charcoal"),
    "黄色": ("黄色", "yellow", "mustard"),
    "粉色": ("粉色", "pink", "rose", "blush"),
    "棕色": ("棕色", "brown", "tan", "camel", "chocolate"),
    "米色": ("米色", "beige", "cream", "oatmeal"),
    "卡其色": ("卡其色", "khaki", "tan", "camel", "sand"),
    "焦糖色": ("焦糖色", "caramel", "tan", "toffee"),
    "紫色": ("紫色", "purple", "violet", "lilac"),
    # 20 条模板审计补齐：细分色名（藏青/墨绿/浅灰）与修饰色（哑光黑/磨砂黑）。
    # 过去「磨砂黑」抽出来是整段 functional，颜色词典没有 key → 永远 ❔。
    "藏青": ("藏青", "navy", "navy blue", "dark blue", "indigo"),
    "墨绿": ("墨绿", "dark green", "forest green", "olive", "deep green"),
    "浅灰": ("浅灰", "light gray", "light grey", "heather gray", "grey"),
    "深棕": ("深棕", "dark brown", "brown", "espresso", "chocolate"),
    "酒红": ("酒红", "burgundy", "wine", "maroon", "oxblood", "red"),
    "哑光黑": ("哑光黑", "matte black", "black", "noir"),
    "磨砂黑": ("磨砂黑", "matte black", "black", "noir"),
    # 2026-11-13 实测：「橄榄绿斜挎小包」抽成约束「橄榄绿」，商品描述里明明写着
    # 「Color/Fabric Type: Olive」却判 ❔ —— 词典只有「绿色→olive」，没有「橄榄绿」这个 key。
    "橄榄绿": ("橄榄绿", "olive", "olive green", "army green", "khaki green"),
    # 2026-11-14 实测：充电宝「深空灰」、保温杯「银灰/米白」三连 ❔ —— 词典只到基础色，
    # 却没有「深空灰/银灰/米白」这些常见修饰色 key。注意用户可能直接写「深空灰」原词，
    # wanted = [k for k in COLOR_KEYWORDS if k in content] 必须能取到这些 key。
    "深空灰": ("深空灰", "dark gray", "dark grey", "gunmetal", "space gray", "space grey", "charcoal"),
    "银灰": ("银灰", "silver", "silver gray", "silver grey", "light gray", "light grey"),
    "米白": ("米白", "ivory", "off-white", "cream", "milky white", "warm white"),
}

# 鞋服/箱包的功能与形态词（2026-11-13 实测：运动鞋/斜挎包两个场景，
# 检索侧只映射出品类词、判定侧这些词全 ❔，等于用户说了 5 条只验了 1 条）。
_APPAREL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "防滑": ("防滑", "non-slip", "nonslip", "anti-slip", "slip resistant", "slip-resistant", "grip", "traction"),
    "透气": ("透气", "breathable", "ventilated", "ventilation", "airflow", "air flow", "air-permeable"),
    "网面": ("网面", "mesh", "mesh upper", "engineered mesh", "knit upper"),
    "拼色": ("拼色", "two-tone", "tonal", "colorblock", "color block", "black white", "black/white"),
    "黑白拼色": ("黑白拼色", "black white", "black/white", "two-tone", "colorblock", "color block"),
    "尺码": ("尺码", "size", "fit"),
    "标准尺码": ("标准尺码", "standard fit", "true to size", "regular fit", "standard size"),
    "斜挎": ("斜挎", "crossbody", "cross-body", "shoulder bag", "messenger", "sling bag", "sling"),
    "尼龙": ("尼龙", "nylon", "cordura", "polyamide"),
    "轻便": ("轻便", "lightweight", "light weight", "light-weight", "ultralight", "featherweight"),
    "通勤": ("通勤", "commuter", "everyday carry", "work bag"),
}
SYNONYM_KEYWORDS.update(_APPAREL_SYNONYMS)

# 版型/长度：同样是客观属性，商品标签里能核对（longline / midi / cropped）。
# 注意不收裸 `long`：`Long Sleeve` 说的是袖子长度，不是衣长，会把短款判成 ✅。
STYLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 2026-11-14 实测：Channel3 结构化属性里版型证据词是外衣写
    # `outerwear-length-type: ['Hip length']`、上衣写 `top-length-type: ['Crop top']`。
    # 词典只收裸 longline/midi/cropped 时，这些真实证据词一个都对不上 —— 又是
    # 「数据里有、词典不认」。hip length ≈ 及臀/中长外套，crop ≈ 短款上衣，都在此补齐。
    "中长款": ("中长", "longline", "midi", "knee-length", "long coat", "hip length", "hip-length"),
    "长款": ("长款", "maxi", "longline", "long coat", "knee-length"),
    "短款": ("短款", "cropped", "short coat", "crop", "crop top"),
    "宽松": ("宽松", "relaxed", "oversized", "loose"),
    "修身": ("修身", "slim fit", "fitted", "tailored", "slim"),
    # 「中长」单独成 key：抽取层口语常省略「款」字（「中长的大衣」→ content=中长），
    # 只有「中长款」key 时 `中长款 in 中长` 匹配不上 → 永远 ❔。
    "中长": ("中长", "longline", "midi", "knee-length", "long coat", "hip length", "hip-length"),
    # 2026-11-14 实测修复：「加大款」→ 抽取层口语常抽成短词「加大」，而「加大」key 的
    # 变体缺 `large` —— 商品 key_features 明明写 "Large capacity" 却判 ❔（「加大款」key
    # 有 large、「加大」没有，用户口语走哪个全看抽取层怎么切）。`large` 是加大款的
    # 直接英文证据，两个字面 key 都得收。（反面：裸 `long` 仍不收，防 Long Sleeve 误判。）
    "加大": ("加大", "oversized", "extra large", "jumbo", "xl", "large"),
}
# 主观质量词：没有评分/口碑字段可核对 → 一律 unknown
SUBJECTIVE_HINTS = (
    "好一点", "更好", "高级", "质感", "好看", "精致", "有档次", "上档次",
    "耐用性更好", "靠谱", "体面", "拿得出手", "有面子",
    "设计感", "音质好", "音质", "品质", "耐用",
)

_PREFIXES = ("不要", "别要", "不能有", "不能", "别", "必须", "需要", "想要", "最好", "要", "有")
_STOP = {
    "的", "和", "或者", "或", "以及", "跟", "与", "一点", "一些", "东西", "商品",
    "预算", "美元", "美金", "元", "块", "usd", "$", "以内", "以下", "左右",
}


def _money(value: float) -> str:
    return f"${float(value):.2f}"


def _text_of(item: Any) -> str:
    """商品的可核对文本：名称 + 品类 + 标签 + **描述**（小写）。

    为什么必须含 description（2026-11-14 实测）：Channel3 商品的证据主战场在描述里
    —— 颜色（"Color/Fabric Type: Olive"）、材料（"nylon shell"）、容量（"10000 mAh
    battery"）经常只出现在 description，name/category/tags 里没有。旧版只拼前三者，
    于是「商品描述里明明写着 Olive/尼龙/10000 mAh 却判 ❔」，等于查得到却证明不了。
    """
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    name = get("name", "") or ""
    category = get("category", "") or ""
    description = get("description", "") or ""
    tags = get("tags", []) or []
    blob = " ".join([str(name), str(category), str(description), *[str(t) for t in tags]])
    return blob.lower()


def _total(items: list[Any]) -> float:
    total = 0.0
    for it in items:
        get = it.get if isinstance(it, dict) else lambda k, d=None: getattr(it, k, d)
        price = float(get("price", 0) or 0)
        quantity = int(get("quantity", 1) or 1)
        total += price * quantity
    return round(total, 2)


def _keywords(content: str) -> list[str]:
    """把约束原文拆成可比对的关键词（剥掉"要/不要/必须"等前缀词）。"""
    text = (content or "").strip()
    if not text:
        return []
    # 数字与单位的空格是排版，不是分隔（2026-11-13 实测）：「68 键」被 `\s` 拆成
    # ["68", "键"]，"68" 又被 isdigit() 过滤 → 只剩裸「键」。判定文案于是变成
    # 「命中关键词「键」」，而且裸 key 会误伤 78 键键盘。先粘回去再拆。
    # 2026-11-14 实测：\d{1,4} 上限 4 位，5 位数字「10000 mAh」粘不上 → 拆成 ["10000","mAh"]，
    # 10000 被过滤、只剩「mAh」，于是关键字列表里没有完整容量串。扩到 2-5 位（68→10000 全覆盖）。
    text = re.sub(r"(\d{2,5})\s+(键|ml|毫升|cm|mm|寸|kg|克|mah|毫安时|wh|瓦|w|v)", r"\1\2", text, flags=re.I)
    parts = re.split(r"[，。,.、；;：:\s（）()【】\[\]\"'“”‘’]+", text)
    out: list[str] = []
    for part in parts:
        # 连词切分共用 text_units 的规则（P0-4）：`低跟` 的「跟」是词素，
        # 无脑 re.split("和|与|跟|以及") 会把它切成关键词「低」，
        # 于是「低跟」永远匹配不上 low heel，判定侧白丢一条证据。
        for seg in split_on_conjunctions(part.strip()):
            seg = seg.strip()
            if not seg:
                continue
            for prefix in _PREFIXES:
                if seg.startswith(prefix) and len(seg) > len(prefix):
                    seg = seg[len(prefix):]
                    break
            seg = seg.strip()
            if not seg or seg in _STOP or seg.isdigit() or seg in out:
                continue
            out.append(seg)
    return out


def _get(obj: Any):
    """统一 dict / dataclass 取值。"""
    if isinstance(obj, dict):
        return lambda key, default=None: obj.get(key, default)
    return lambda key, default=None: getattr(obj, key, default)


def _synonym_variants(kw: str, context: str | None = None) -> tuple[str, ...]:
    """约束词 → 可比对的证据词：先收精确 key 的变体，再收「词典 key 是该词子串」的变体，
    最后回退约束词本身。为什么两波都要：根因（评审 P0-2）——「高腰版型」是整词，
    精确 key 取不到（key 是「高腰」）→ 子串波兜住；「水洗蓝」虽有精确 key，但变体是
    washed blue（蓝色词），商品描述写 Washed denim（工艺词）→ 还要靠「水洗」的子串
    变体 washed 命中。两波合并，同义不同写法都能对上。

    `context`：约束原文。「68 键」被 _keywords 拆词时丢了数字（「68」当纯数字过滤），
    所以数字键位必须从原文里取（68-key / 68 keys），否则会退化成裸「键」→ 误伤 78 键。"""
    merged: list[str] = []
    exact = SYNONYM_KEYWORDS.get(kw)
    if exact:
        merged.extend(exact)
    m = re.search(r"(\d{1,3})\s*键", context or kw)
    if m:
        n = m.group(1)
        merged.extend([f"{n}-key", f"{n} key", f"{n} keys", f"{n}key"])
    # 容量/规格数字同理：{500ml} 被 _keywords 保留成整串，但商品写 "500 ml"/"500ml" 排版可能不同，
    # 中间有没有空格不该决定能不能核对（2026-11-10 审计）。
    # 2026-11-14 实测扩展：原正则只认 ml/毫升，且 \d{2,4} 上限 4 位 —— 用户约束「10000mAh」永远只拿到
    # 原文自身，商品写 "10000 mAh battery"（带空格）就失配 → 充电宝案例 10000mAh 恒 ❔。
    # 现扩到 mah/毫安时/wh/瓦时/w/瓦 六类单位 + 5 位数字，变体同时生成 无空格/有空格/连字符 三种写法。
    m = re.search(r"(\d{2,5})\s*(ml|毫升|mah|毫安时|wh|瓦时|w|瓦|watt)", (context or kw).lower())
    if m:
        n = m.group(1)
        unit = m.group(2)
        merged.extend([f"{n}{unit}", f"{n} {unit}", f"{n}-{unit}"])
    for key, variants in SYNONYM_KEYWORDS.items():
        if key and len(key) >= 2 and key != kw and key in kw:
            merged.extend(variants)
    return tuple(dict.fromkeys(merged)) or (kw,)


def _find_hit(blob: str, words: tuple[str, ...] | list[str]) -> str | None:
    """在商品文本（已小写）里找第一个命中的证据词，返回命中的那个词。

    英文用词边界匹配：`red` 必须命中 "Dark Red" 而不能命中 "colored"；
    `anc` 不能命中 "dance"。中文直接子串（没有分词问题，且中文词本身有区分度）。
    """
    for word in words:
        w = (word or "").strip().lower()
        if not w:
            continue
        if w.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", blob):
                return w
        elif w in blob:
            return w
    return None


def _name_of(item: Any, fallback: str = "") -> str:
    return str(_get(item)("name", "") or fallback)


def _judge_constraint(constraint: Any, items: list[Any], budget_limit: float | None) -> dict:
    """单条约束的组合级判定：sat / unsat / unknown + 证据或原因。

    这是「整体满足 N/M 条」与逐条 ✅/❌/❔ 的唯一数据源。
    每条判定携带：rule（规则表条目）、scope（combo=组合层）、
    预算还会带 limit（该约束的数字上限，供前端直接展示）。
    """
    get = _get(constraint)
    ctype = get("type", "functional") or "functional"
    content = str(get("content", "") or "")
    base = {
        "constraint_id": get("id", "") or "",
        "type": ctype,
        "content": content,
        "source": get("source", "") or "",
        "hard": bool(get("hard", False)),
        "rule": RULES.get(ctype, f"type.{ctype or 'unknown'}.unstructured"),
        "scope": "combo",  # 组合层判定；逐件判定走 items[].results
        "limit": None,
    }

    # ── 预算：数字比较。scope 决定比什么 ─────────────────────────────
    # Bug 1（评审坑 3）：「单件预算不超 130」scope=per_item 约束**每一件**单价，
    # 不是组合总价。total 看 Σprice ≤ limit，per_item 看 max(price) ≤ limit。
    if ctype == "budget":
        limit = float(get("numeric_value") or budget_limit or 0)
        base["limit"] = round(limit, 2) if limit else None
        base["scope"] = get("scope", "total") or "total"
        if not limit:
            return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                    "reason": "预算约束没有可比较的金额，无法判定"}
        if base["scope"] == "per_item":
            prices = [float(_get(it)("price", 0) or 0) for it in items]
            if not prices:
                return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                        "reason": "候选池为空，无法逐件判定单件预算"}
            worst = max(prices)
            if worst <= limit:
                return {**base, "status": STATUS_SAT,
                        "evidence": f"全部 {len(prices)} 件单价 ≤ {_money(limit)}（最高 {_money(worst)}）",
                        "reason": None}
            return {**base, "status": STATUS_UNSAT,
                    "evidence": f"最高单价 {_money(worst)} > {_money(limit)}（超 {_money(worst - limit)}）",
                    "reason": "存在单价超过单件预算的商品"}
        total = _total(items)
        if total <= limit:
            return {**base, "status": STATUS_SAT,
                    "evidence": f"{_money(total)} ≤ {_money(limit)}", "reason": None}
        return {**base, "status": STATUS_UNSAT,
                "evidence": f"{_money(total)} > {_money(limit)}（超支 {_money(total - limit)}）",
                "reason": "组合总价超过预算上限"}

    # ── 主观质量：无评分/口碑字段 → 绝不假装 ✅ ──────────────────────
    if any(hint in content for hint in SUBJECTIVE_HINTS):
        return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                "reason": "主观质量无法从商品字段判定（缺评分/口碑数据），请人工确认"}

    keywords = _keywords(content)
    if not keywords:
        return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                "reason": "没有可比对的关键词"}

    # ── 禁忌：任一商品命中禁忌词 → 组合确证不满足 ────────────────────
    if ctype == "taboo":
        for kw in keywords:
            for it in items:
                if kw in _text_of(it):
                    return {**base, "status": STATUS_UNSAT,
                            "evidence": f"「{_name_of(it, kw)}」命中禁忌词「{kw}」",
                            "reason": "组合里出现了明确被禁止的商品"}
        return {**base, "status": STATUS_SAT,
                "evidence": f"未出现禁忌词：{'、'.join(keywords)}", "reason": None}

    # ── 材质：词典命中即判；未命中 → unknown（没有证据 ≠ 违反）──────
    materials = [m for m in MATERIAL_KEYWORDS if m in content]
    if materials:
        first_hit: tuple[Any, str, str] | None = None
        for it in items:
            blob = _text_of(it)
            for mat in materials:
                hit_word = next((w for w in MATERIAL_KEYWORDS[mat] if w in blob), None)
                if hit_word:
                    first_hit = (it, mat, hit_word)
                    break
            if first_hit:
                break
        if first_hit:
            it, mat, word = first_hit
            return {**base, "rule": RULES["material"], "status": STATUS_SAT,
                    "evidence": f"「{_name_of(it, word)}」材质命中「{mat}」（{word}）",
                    "reason": None}
        return {**base, "status": STATUS_UNKNOWN, "rule": RULE_NO_EVIDENCE,
                "evidence": None,
                "reason": f"组合中没有任何商品命中材质「{'、'.join(materials)}」——"
                          "可能是目录没覆盖该材质，不能据此判定不满足，请人工确认"}

    # ── 环保：Channel3 没有 eco_label，靠标签词典；无证据即 unknown ──
    if any(hint in content.lower() for hint in ECO_CONTENT_HINTS):
        for it in items:
            blob = _text_of(it)
            hit_word = next((w for w in ECO_ITEM_EVIDENCE if w in blob), None)
            if hit_word:
                return {**base, "rule": RULES["eco"], "status": STATUS_SAT,
                        "evidence": f"「{_name_of(it, hit_word)}」标签命中环保（{hit_word}）",
                        "reason": None}
        return {**base, "status": STATUS_UNKNOWN, "rule": RULE_NO_EVIDENCE,
                "evidence": None,
                "reason": "没有任何商品带环保标识（recycled / 环保 / 有机 / 竹纤维…）——"
                          "可能只是商品描述没写全，不能据此判定不满足，请人工确认"}

    # ── 颜色 / 版型：偏好里的客观属性，命中即 ✅；未命中 → ❔ ─────────
    # 这两类过去一律落到 preference.unverifiable（"没有可结构化比对的字段"），
    # 但颜色和版型明明能从商品标签核对（"Dark Red" / "Longline cut"）。
    # 未命中仍然是 ❔ 而不是 ❌：描述里没写颜色，不代表不是红色。
    for label, lexicon, rule in (
        ("颜色", COLOR_KEYWORDS, "attribute.color"),
        ("版型", STYLE_KEYWORDS, "attribute.style"),
    ):
        wanted = [k for k in lexicon if k in content]
        if not wanted:
            continue
        for it in items:
            blob = _text_of(it)
            for want in wanted:
                hit = _find_hit(blob, lexicon[want])
                if hit:
                    return {**base, "rule": rule, "status": STATUS_SAT,
                            "evidence": f"「{_name_of(it, hit)}」命中{label}「{want}」（{hit}）",
                            "reason": None}
        return {**base, "status": STATUS_UNKNOWN, "rule": RULE_NO_EVIDENCE,
                "evidence": None,
                "reason": f"组合中没有任何商品标注{label}「{'、'.join(wanted)}」——"
                          f"可能只是商品描述没写{label}，不能据此判定不满足，请人工确认"}

    # ── 功能/品类：名称·品类·标签关键词命中（中英同义扩展）；未命中 → unknown ──
    if ctype == "functional":
        for kw in keywords:
            variants = _synonym_variants(kw, content)
            for it in items:
                blob = _text_of(it)
                hit = _find_hit(blob, variants)
                if hit:
                    detail = f"「{kw}」" if hit == kw else f"「{kw}」（{hit}）"
                    return {**base, "rule": RULES["functional"], "status": STATUS_SAT,
                            "evidence": f"「{_name_of(it, hit)}」命中关键词{detail}",
                            "reason": None}
        return {**base, "status": STATUS_UNKNOWN, "rule": RULE_NO_EVIDENCE,
                "evidence": None,
                "reason": f"组合中没有任何商品命中关键词「{'、'.join(keywords)}」——"
                          "可能是目录没覆盖或描述缺字段，不能据此判定不满足，请人工确认"}

    # ── 其它类型：无结构化字段可比 → unknown（进「系统理解到的意图」）──
    return {**base, "status": STATUS_UNKNOWN, "evidence": None,
            "reason": f"约束类型「{ctype}」没有可结构化比对的字段"}
def item_satisfies(constraint: Any, item: Any) -> bool:
    """单件商品是否满足某条约束（供 planner 回填 PlanItem.matched_constraints）。"""
    rows = _item_verdicts(constraint, [item])
    return bool(rows) and rows[0]["status"] == "pass"



def _item_verdicts(constraint: Any, items: list[Any]) -> list[dict]:
    """单条约束的逐件判定：pass / fail / n/a。

    预算是组合级属性，逐件一律 n/a —— 把「$285 ≤ $350」复制到每件上会让证据贬值。
    """
    get = _get(constraint)
    ctype = get("type", "functional") or "functional"
    content = str(get("content", "") or "")

    if ctype == "budget":
        # 组合级 total 预算：逐件一律 n/a（把「$285 ≤ $350」复制到每件上会让证据贬值）。
        # 单件 per_item 预算（Bug 1）：逐件有真值 —— 单价 ≤ 上限是 pass，否则 fail。
        if (get("scope", "total") or "total") == "per_item":
            limit = float(get("numeric_value") or 0)
            if not limit:
                return [{"status": "n/a", "evidence": "无金额可判"} for _ in items]
            rows = []
            for it in items:
                price = float(_get(it)("price", 0) or 0)
                rows.append({
                    "status": "pass" if price <= limit else "fail",
                    "evidence": f"单价 {_money(price)} ≤ {_money(limit)}" if price <= limit
                               else f"单价 {_money(price)} > {_money(limit)}",
                })
            return rows
        return [{"status": "n/a", "evidence": "组合级判定"} for _ in items]
    if any(hint in content for hint in SUBJECTIVE_HINTS):
        return [{"status": "n/a", "evidence": "不可判定"} for _ in items]

    keywords = _keywords(content)
    if not keywords:
        return [{"status": "n/a", "evidence": "不可判定"} for _ in items]

    rows: list[dict] = []
    is_eco = any(hint in content.lower() for hint in ECO_CONTENT_HINTS)

    # 组合级判定用的同一套词典，逐件判定也必须一致 —— 否则会出现
    # 「组合说 ✅ 已满足，逐件表却全 ❌」这种自相矛盾的界面。
    expansion: list[str] = list(keywords)
    for kw in keywords:
        expansion.extend(_synonym_variants(kw, content))
    for lexicon in (COLOR_KEYWORDS, STYLE_KEYWORDS, MATERIAL_KEYWORDS):
        for want, words in lexicon.items():
            if want in content:
                expansion.extend(words)
    expansion = list(dict.fromkeys(expansion))

    if ctype == "taboo":
        for it in items:
            blob = _text_of(it)
            hit = _find_hit(blob, keywords)
            rows.append({"status": "fail" if hit else "pass",
                         "evidence": f"命中禁忌词「{hit}」" if hit else "未出现禁忌词"})
        return rows

    for it in items:
        blob = _text_of(it)
        hit_kw = _find_hit(blob, expansion)
        if hit_kw:
            rows.append({"status": "pass", "evidence": f"标签/名称命中：{hit_kw}"})
            continue
        if is_eco:
            eco_word = next((w for w in ECO_ITEM_EVIDENCE if w in blob), None)
            if eco_word:
                rows.append({"status": "pass", "evidence": f"标签命中：{eco_word}"})
                continue
        rows.append({"status": "fail", "evidence": "未命中该约束关键词"})
    return rows


def evaluate(
    constraints: list[Any],
    items: list[Any],
    *,
    budget_limit: float | None = None,
    max_failures_shown: int = 2,
) -> dict:
    """组合级 + 逐件级的满足度总账（纯计算，无副作用）。

    `max_failures_shown`：❌ 最多显 2 条（满屏红会让人怀疑系统），
    超出的不删除、只标 `visible=False` 并记入 `hidden_failures` —— 数据完整，展示克制。
    """
    active = [c for c in (constraints or []) if (_get(c)("status", "active") or "active") == "active"]
    items = list(items or [])

    verdicts: list[dict] = []
    for c in active:
        verdicts.append(_judge_constraint(c, items, budget_limit))

    passed = sum(1 for v in verdicts if v["status"] == STATUS_SAT)
    failed = sum(1 for v in verdicts if v["status"] == STATUS_UNSAT)
    unknown = sum(1 for v in verdicts if v["status"] == STATUS_UNKNOWN)

    shown_failures = 0
    for v in verdicts:
        if v["status"] == STATUS_UNSAT:
            shown_failures += 1
            v["visible"] = shown_failures <= max_failures_shown
        else:
            v["visible"] = True

    hidden = [v["constraint_id"] for v in verdicts if not v["visible"]]

    item_rows: list[dict] = []
    for idx, it in enumerate(items):
        name = _name_of(it)
        item_id = _get(it)("id", "") or f"item-{idx}"
        results = []
        for c, v in zip(active, verdicts):
            row = _item_verdicts(c, [it])[0]
            if v["status"] == STATUS_UNKNOWN:
                row = {"status": "n/a", "evidence": "不可判定"}
            results.append({
                "constraint_id": v["constraint_id"],
                "label": v["content"],
                "type": v["type"],
                **row,
            })
        item_rows.append({
            "item_id": item_id,
            "name": name,
            "price": round(float(_get(it)("price", 0) or 0), 2),
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "failed": sum(1 for r in results if r["status"] == "fail"),
            "results": results,
        })

    total = len(verdicts)
    headline = f"满足 {passed}/{total} 条" if total else "还没有可判定的约束"
    if unknown:
        headline += f" · {unknown} 条无法判定"

    return {
        "judgeable": bool(total),
        "total": total,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "headline": headline,
        "max_failures_shown": max_failures_shown,
        "hidden_failures": hidden,
        "constraints": verdicts,
        "items": item_rows,
    }

