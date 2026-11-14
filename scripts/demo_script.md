# 演示脚本（录制前定稿，2026-11-10）

> 规则：只演示抽查的 query（每条覆盖一个演示点）；每条先跑通再录；
> 哪条 0 命中只补那一条的映射词，**不无限补词表**。

## 演示组（从用户提供的 8 条团购 + 12 条单人里抽查）

### A. 团购组（演示：团购语义 + 大数量 + 单件预算 per_item + 描述词）

| # | Query（中文） | 数量 | 单件预算 | 演示点 |
|---|---|---|---|---|
| A1 | 我们团体采购一批通勤外套，挺括抗皱，藏青色，中长款，单件预算不超 320，一共采购 28 件，用于团队统一工装。 | 28 件 | ≤320 | 团购大数量、材质描述词（挺括抗皱）、颜色（藏青） |
| A2 | 我们团体采购一批无线蓝牙耳机，音质清晰低延迟，哑光黑，小巧入耳款，单件预算不超 160，一共采购 15 副，发给组员日常办公听会议。 | 15 副 | ≤160 | 主观词清洗（音质→❔）；描述词（清晰低延迟/入耳）；数量 15 副 |
| A3 | 我们团体采购一批双肩背包，耐磨防水面料，深灰色，大号，单件预算不超 180，一共采购 22 个，外出活动统一使用。 | 22 个 | ≤180 | 描述词（耐磨防水面料）；数量 22 个 |
| A4 | 我们团体采购一批保温杯，316不锈钢，米白色，500ml，单件预算不超 90，一共采购 35 只，作为团队伴手礼。 | 35 只 | ≤90 | 材质（316不锈钢）；规格（500ml）；数量 35 只 |
| A5 | 我们团体采购一批静音键盘，按键顺滑，浅灰色，68键，单件预算不超 130，一共采购 12 把，分配给开发工位。 | 12 把 | ≤130 | 数量词「把」；描述词（静音/顺滑）；Bug 1（per_item 预算）修复展示 |

### B. 单人组（演示：单人单品 + 服饰/数码/生活分类）

| # | Query（中文） | 演示点 |
|---|---|---|
| B1 | 我需要一条牛仔裤，高腰版型，水洗蓝，直筒，不容易掉色。 | 服饰类；客观属性（高腰/水洗蓝/直筒） |
| B2 | 我需要一条围巾，羊毛混纺，焦糖色，宽版，冬天保暖。 | 生活类；材质（羊毛）+ 颜色（焦糖）+ 功能（保暖） |
| B3 | 我需要一个充电宝，轻薄便携，白色，小容量，出门应急充电。 | 数码类；已验证 Channel3 通 → $13.89 Portable Charger Power Bank |
| B4 | 我需要一只马克杯，陶瓷材质，奶白色，宽口，泡茶不烫手。 | 生活类；材质（陶瓷）+ 颜色（奶白）；预期 Channel3 empty → 本地降级诚实说明 |

## 已验证结果（2026-11-10 .tmp\demo_q_probe.py）

```
== 检索词 + 数量解析 ==
  团购一批通勤外套…28 件 → qty=28 en=['jacket']
  团购一批无线蓝牙耳机…15 副 → qty=15 en=['headphones','bluetooth','wireless']
  团购一批双肩背包…22 个 → qty=22 en=['backpack','gray','grey']
  团购一批保温杯…35 只 → qty=35 en=['stainless','steel','insulated','cup']
  牛仔裤… → qty=1 en=['jeans','high','rise','washed','blue','straight','leg']
  围巾… → qty=1 en=['scarf','wool','caramel','wide','warm','thermal']
  马克杯… → qty=1 en=[]（无英文映射 → Channel3 empty 降级本地，诚实说明）
  充电宝… → qty=1 en=['power','bank','portable']

== Channel3 e2e（真实网络） ==
  通勤外套 → feasible=True src=channel3 nA=20（qty=28 → 数量不足置 quantity_shortfall）
  无线蓝牙耳机 → feasible=True src=channel3 nA=15 ✓
  双肩背包 → feasible=True src=channel3 nA=20（qty=22）
  保温杯 → feasible=True src=channel3 nA=19（qty=35）
  牛仔裤 → feasible=True src=channel3 nA=1 totalA=51.0 ✓
  围巾 → feasible=True src=channel3 nA=1 totalA=19.99 ✓
  马克杯 → feasible=True src=k1_catalog（Channel3 empty → 本地降级诚实说明）
  充电宝 → feasible=True src=channel3 nA=1 totalA=13.89 ✓

== 本地 e2e（降级路径） ==
  全部 feasible=True src=k1_catalog nA=按 qty cap（本地目录无对应品类时不再 0 命中返空）
```

## 录制注意事项（演示环境陷阱）

- PowerShell `>` 重定向写 UTF-16 —— 探针/脚本一律用 **argv 文件模式**（`python x.py --out out.txt`）
- 重启服务前：`Remove-Item Env:LLM_MODE,Env:PAYMENT_MODE`
- `cwd=D:\ecv-paypal`，后端端口 8000
- **只允许一个监听进程**（双实例会拍到旧代码，见 preflight_demo.py）