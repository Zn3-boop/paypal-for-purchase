# 团体采购协理 · 实现清单（Execution Checklist）

> 状态图例：✅ 已完成并验证 ｜ 🟡 已有底座、需增强/补全 ｜ 🔴 全新开发
> 差距盘点：2026-11-01 · 方法：源码审计（apps/server + apps/web/src）+ tsc/lint 验证

## 一、任务总览表

| 模块 | 内容 | 优先级 | 实际状态 | 差距摘要 |
|---|---|---|---|---|
| A | 约束系统（状态对象 + 看板） | P0 | ✅ A1-A5 | 看板支持新增/删除自然语言、候选消歧与卡片 × 删除；删除型指令不进 LLM |
| B | 解析 Prompt 组 | P0 | ✅ | 三条铁律 + 提取/冲突/变更 Prompt 齐全 |
| C | 双方案生成（权衡+风险） | P0 | ✅ | 前置过滤 + A/B 生成 + Diff 视图（已增强） |
| D | 预算 Guardrail | P0 | ✅ | 方案生成/改单/支付前均显式 `check_budget`；capture 回调增加实扣金额副门 |
| E | HITL 自然语言改单 | P0 | ✅ | 五类指令解析 + 执行链路 + diff 回显 |
| F | HITL 复核面板 | P0 | ✅ | 确认弹窗已渲染分摊预览（F2）与 F3 风险区 |
| G | 多人分摊 + PayPal 支付 | P0 | ✅ | split_bill + 沙盒回调闭环 + 取消分支 |
| H | 订单生命周期 | P1 | ✅ H1-H4 | 状态机+API+档案页+自然语言查单全通 |
| I | 决策审计日志 | P1 | ✅ | Schema + 8 埋点 + 时间轴 UI |
| J | 兜底逻辑（无解提示） | P0 | ✅ | 禁忌/预算/通用三分支 + 统一模板 |
| K | 演示工程 | P0 | 🟡 | K1-K3 + K5 ✅；K4 录屏未做 |

## 二、模块明细

### A. 约束优先架构
- [x] ✅ **A1 定义 Constraint 数据结构** — `services/group_purchase/constraints.py` Constraint dataclass（id/type/content/hard/source/status + numeric_value/category）
- [x] ✅ **A2 全局约束状态对象** — `ConstraintManager` + `session.constraints`（session_state.py），会话级增删改查/按来源分组
- [x] ✅ **A3 约束看板 UI** — `ConstraintsDashboard.tsx` 按类型分组展示（预算/功能/禁忌/偏好四色徽章）
- [x] ✅ **A4 冲突高亮** — `detect_conflicts()` 预算比值冲突（ratio≥1.4）+ 前端红框/冲突列表
- [x] ✅ **A5 看板内自然语言增删约束入口** — `ConstraintsDashboard.tsx`：新增走 `/constraints/extract`；删除型指令（删除/删掉/移除/去掉…）只在本地匹配已有结构化约束后调 `DELETE /constraints/{id}`，**绝不送 LLM extract**；0 命中给提示、多命中出候选按钮（不静默删）、删除+新增混合指令拒执行；每张卡片另有 × 兜底删除。`constraintDelete.ts` 承载纯匹配逻辑（2026-11-03 验证）。后端 `GET /constraints` 已过滤 `removed`，`planner.prefilter_by_constraints` / `/plans` 预算提取 / `cart_modifier` 预算调整均只消费 `status=="active"`

### B. 解析 Prompt 组
- [x] ✅ **B1 主控 System Prompt（三条铁律）** — `prompts.py` GROUP_PURCHASE_SYSTEM_PROMPT（①约束优先于检索 ②无解必须直说 ③AI 无支付发起权）
- [x] ✅ **B2 约束提取** — `B2_CONSTRAINT_EXTRACTION_PROMPT` + `ExtractedConstraint` schema + `constraints.py` /extract（LLM→规则降级）
- [x] ✅ **B3 冲突识别** — `B3_CONFLICT_DETECTION_PROMPT` + `ConflictDetectionOutput`（budget_mismatch / functional_taboo / preference_oppose + severity）
- [x] ✅ **B4 约束变更解析** — `B4_CHANGE_PARSING_PROMPT` + `ConstraintChangeParsingOutput`（relax/tighten/remove/add/modify）

### C. 双方案生成（Trade-off Aware）
- [x] ✅ **C1 检索层适配** — `planner.prefilter_by_constraints()` 硬约束前置过滤（非后置）
- [x] ✅ **C2 方案生成** — `DualPlanOutput` schema + `planner._assemble_strategy_plan()`（低价 A / 耐用 B + matched/unmet/trade_offs/risks）
- [x] ✅ **C3 前端双方案对比视图** — `PlansTab.tsx`（2026-11-01 增强：①左右双卡 ②价格高亮+预算进度条 ③商品折叠清单 ④sacrificed_members 红徽章 ⑤Guardrail 黄警示 ⑥Diff 对比视图 ⑦带金额确认弹窗）
- [x] ✅ **C4 牺牲了谁的诉求（成员维度）** — `sacrificed_members`（由 unmet_constraints 来源推导）+ `trade_offs`（"牺牲了 {source} 的偏好：…"）

### D. 预算 Guardrail
- [x] ✅ **D1 calc_total()** — `budget_guardrail.py`
- [x] ✅ **D2 check_budget() 强制三处** — 方案生成后（plans.py 对 A/B 各一次）✅；每次改单后（cart_modifier.py）✅；**支付前**（paypal_callback.create_order 在 `confirmed → paying` 状态机转移守卫中显式调用 `check_budget`）✅ + capture 回调副门（实扣金额 == 订单金额，不符落 cancelled + 告警）✅
- [x] ✅ **D3 超预算策略** — `find_cheaper_alternatives()` + `adjust_cart_to_budget()` + 前端替换建议展示（💡 原→替换 省 $X）

### E. HITL 自然语言改单
- [x] ✅ **E1 意图解析** — `CartModificationOutput` / `E1_CART_MODIFICATION_PROMPT`（swap/delete/add/adjust_budget/adjust_constraint）+ LLM 失败规则降级
- [x] ✅ **E2 执行链路** — `execute_modifications()`：解析→更新购物车→重算→Guardrail→分摊→审计
- [x] ✅ **E3 diff 回显** — `cart.py /cart/modify` 返回 diff{old_total,new_total,changed_items} + "总价变化 +X.XX" + intent_summary

### F. HITL 复核面板
- [x] ✅ **F1 汇总（清单+单价/数量/总价）** — PlansTab ConfirmationModal 有总价/件数/风险；团购"最终清单复核面板"即确认弹窗（HITL 流程内）
- [x] ✅ **F2 分摊预览（每人应付金额）** — `SplitConfirmationModal` 拉 `/cart/split_preview`，渲染每人应付 + adjustment_member 尾差；带 loading/error 与取消竞态保护（2026-11-03）
- [x] ✅ **F3 风险提示汇总区** — 方案卡 + 确认弹窗展示 risks / trade_offs / sacrificed_members（2026-11-01 增强）
- [x] ✅ **F4 唯一支付触发点** — B1 铁律③ + HITL 审批闸门；create_order 不信任前端 amount（强制 session cart_total）——金额漏洞已修复
- [x] ✅ **F5 权限边界写进文档** — README 新增 “Group Purchase mode: the boundary is even stricter” 段落：约束优先/删除不进 LLM、唯一支付触发点与金额服务端重算、实扣副门、只读查单归属、审计留痕（2026-11-03）
### G. 分摊 + PayPal 支付
- [x] ✅ **G1 split_bill()** — `split_bill.py`：均摊 + 尾差（最后一人承担小数差，Decimal 四舍五入）
- [x] ✅ **G2 PayPal 沙盒分账模拟** — `simulate_paypal_split_payment()`（主账号收款 + payout_schedule 记录）
- [x] ✅ **G3 支付链路** — `paypal_callback.py`：create_order（含 session_id 透传 + review_confirmed/payment_completed 审计埋点）→ callback（capture + 302 前端落地页）→ cancel
- [x] ✅ **G4 支付失败/取消分支** — /cancel 跳转 `/payment/review?status=cancelled`，`PaymentResult.tsx` 渲染"已取消支付"落地页

### H. 订单生命周期（已完成状态机 + API 后端）
- [x] ✅ **H1 状态机** — `order_lifecycle.py`：`draft → confirmed → paying → paid` + `cancelled` 分支，含 4 个转移守卫（review_confirmed 审计存在 / check_budget PASS / 实扣金额一致 / 取消），非法转移与守卫失败在结构上无法发生
- [x] ✅ **H2 状态推进 + 订单 API（四个）** — `app/api/orders.py`：`GET /orders`、`GET /orders/{id}`（含关联审计事件）、`GET /orders/{id}/status`（轻量轮询）、`POST /orders/{id}/cancel`（paid 之前可取消）；`paypal_callback` 的 create/callback/cancel 三端点已接入状态机。archive 的 H3 前端已消费（见 H3）
- [x] ✅ **H3 订单档案页** — `OrdersTab.tsx` 第 4 面板：会话过滤列表、支付中才 4s 轮询、订单详情自动展开（PayPal success CTA 携带 `order_id`）、商品/状态机快照，并复用 `auditEventMeta.ts` + `auditEventSummary.tsx` 渲染关联审计事件；`GET /orders` 与 status 均强制 `session_id`（2026-11-03 验证）
- [x] ✅ **H4 自然语言查单** — `order_query.py` + `POST /orders/query?session_id=...`：只读解析（单号/“最近”/歧义列出），`session_id` 由服务端注入并强制订单归属，跨会话单号统一回“没有找到”不泄露存在性；`OrdersTab.tsx` 顶部提供查单输入与快捷句并自动展开命中订单（2026-11-03 验证）

### I. 决策审计日志
- [x] ✅ **I1 日志 schema** — `audit_log.py` AuditLogEntry（timestamp/round_num/event/session/user/actor/data/constraint_snapshot/plan_snapshot），JSONL 持久化
- [x] ✅ **I2 五个（实际 9 个）埋点** — constraint_changed / plan_generated / cart_modified / review_confirmed / payment_completed / conflict_detected / budget_violated / fallback_triggered / **order_status_changed**（H1 状态机每次转移落一条，带 from/to/order_id/reason，2026-11-01 新增）全部有 `log_*()` 函数
- [x] ✅ **I3 审计台账 UI** — `AuditTimeline.tsx` 时间轴（五核心事件高亮 + 展开明细 + 4s 轮询）

### J. 兜底逻辑
- [x] ✅ **J1 硬约束违反 → 无法满足 + 二选一建议** — `fallback.generate_fallback_message()`：禁忌冲突 / 预算过低 / 通用无解三分支，均附 suggestions[]（二选一）
- [x] ✅ **J2 统一模板** — `FALLBACK_TEMPLATES`（hard_constraint_violation / budget_insufficient / no_valid_combination）+ `J1_FALLBACK_PROMPT`

### K. 演示工程
- [x] ✅ **K1 Mock 商品池** — `shopper/data/k1_catalog.json`：45 件（装饰 14 / 奖品 12 / 饮用水 11 / 文具 8），环保款 22，价格 1.5~95，含环保/普通对照
- [x] ✅ **K2 降级实测** — `planner._retrieve_catalog()`：Channel3 异常 try/except → 自动切 K1（use_channel3 可控）
- [x] ✅ **K3 完整 5 步 + 兜底分支复跑** — `golden_path_verify.py`（fake 网关）：其自身最短路径基线 8 事件严格匹配（constraint→plan→cart→review→draft→confirmed→paying→payment_completed→paid）、异常路径金额篡改触发 `payment_amount_mismatch` + cancelled，快照存档 `docs/golden_path_timeline_<stamp>.json`（utf-8 直写，2026-11-04 重生成）；数字统一由 `scripts/calc_demo_numbers.py` 输出并自断言：方案 A 8 件 $26.0 / 方案 B 8 件 $393.0（预算 600），兜底触发预算 $350（B 购物车 393.0 超支 43.0，D2 主门 400 `budget_violated`），删 3 件省 $108.0 → $285.0；预算 1.0 无解（`budget_too_low` + 2 条二选一建议）；Channel3 断网异常自动降级 K1 45 件（2026-11-04 复核）
- [ ] 🔴 **K4 录屏 + 备份视频** — 未做
- [x] ✅ **K5 Devpost 文案** — `docs/DEVPOST_SUBMISSION.md`：问题/方案、五条技术亮点、3 分钟演示路径、验证证据与诚实限制（2026-11-03）
### K3 权威事件序列（2026-11-04 探针实测，唯一口径）

> 结论：golden 最短路径的 8 事件**不是**完整 K3 主线的子序列——K3 里"删 3 件"发生在预算门
> 400 之后，`cart_modified` 必然晚于 `review_confirmed`（复核在预算门之前落账）。因此验收
> **不用事件条数对齐**，只用下面三条口径。`scripts/demo_seed_script.md` 引用本表，不复写。

1. **类型覆盖**：`MISSING = []`（8 种基线事件类型一个不缺）。
2. **生命周期严格有序**：`review_confirmed` < `order_status_changed(draft→confirmed)` <
   `order_status_changed(confirmed→paying)` < `payment_completed` < `order_status_changed(paying→paid)`。
3. **额外事件与操作一一对应**：`conflict_detected`（350 新增时与 600 并存，比例 1.7x）/
   `budget_violated` + `fallback_triggered`（D2 门 400，overage 43.0）/ `cart_modified`（删 3 件）。

主线 5 步完整序列（12 事件）：

| # | 事件 | 关键数据 |
|---|---|---|
| 1 | `constraint_changed` | added 600 |
| 2 | `plan_generated` | A $26.0 / 8 件，B $393.0 / 8 件 |
| 3 | `conflict_detected` | 600 vs 350（比例 1.7x） |
| 4 | `constraint_changed` | added 350 |
| 5 | `review_confirmed` | final_total **393.0**，plan_id B（第 1 次点"确认支付"落账） |
| 6 | `order_status_changed` | draft→confirmed |
| 7 | `budget_violated` | limit 350.0 / actual 393.0 / resolution 超支 43.00 |
| 8 | `fallback_triggered` | reason=支付前预算守卫拦截 393.00 > 350.00 |
| 9 | `cart_modified` | batch_update ids=3，393.0→285.0 |
| 10 | `order_status_changed` | confirmed→paying |
| 11 | `payment_completed` | final_total **285.0** |
| 12 | `order_status_changed` | paying→paid |

取消分支（**必须全新 session**）：`paying→cancelled` 转移 + 取消后重建单的**第二次**
`review_confirmed`（该 session 共 11 事件，OrdersTab 一屏 cancelled + paid 双终态）。
同 session paid 后再 `create_order` → HTTP 409「订单已支付，禁止重复创建支付订单」（预期治理行为）。

## 三、剩余工作清单（按缺口聚合）

### P0 缺口
| # | 缺口 | 涉及 | 备注 |
|---|---|---|---|
| 1 | ~~F2 分摊预览前端展示~~ | F | ✅ 确认弹窗已渲染（每人均摊 + 尾差成员） |
| 2 | ~~D2 支付前显式 check_budget~~ | D | ✅ create_order 守卫 + 审计 |
| 3 | ~~A5 约束看板自然语言输入入口~~ | A | ✅ 增/删 + 候选消歧 + 卡片 × |
| 4 | ~~F5 README 补团购权限边界段落~~ | F | ✅ 已写入 README |

### P1 缺口（H 模块）
| # | 缺口 | 依赖 |
|---|---|---|
| 1 | ~~H1 订单状态机~~ | ✅ 已完成（order_lifecycle.py） |
| 2 | ~~H2 状态推进 + 订单 API~~ | ✅ 已完成（app/api/orders.py 四个端点 + paypal 三端点接线） |
| 3 | ~~H3 订单档案页~~ | ✅ 已完成（列表 + 详情自动展开 + 审计关联 + paying 条件轮询） |
| 4 | ~~H4 自然语言查单~~ | ✅ 已完成（`/orders/query` 只读 + 归属强制 + 前端快捷句） |

### Demo 收尾
- ~~K3 完整流程复跑（旧 6 步话术 + 预算 350 兜底 + 断网降级）~~ ✅ 2026-11-04（口径已修订，见下行）
- K3 主线按**修订口径**复跑：5 步主线 + 12 事件三条验收（`scripts/demo_seed_script.md` 已同步）+ 新 session 取消分支
- K4 录屏（含备份视频）
- ~~K5 Devpost 文案（定位段 + 技术亮点五条）~~ ✅ `docs/DEVPOST_SUBMISSION.md`

## 四、开发顺序建议（剩余 3 批）

**批 1 · 后端纯函数（半天）**：H1+H2 状态机（含 check_budget 支付前调用 D2）→ F2 分摊接口若缺再补
**批 2 · 前端（一天）**：A5 约束看板输入 → F2 分摊展示（确认弹窗 + 订单页）→ H3 订单档案页（App.tsx 第 4 面板）
**批 3 · 收尾**：H4 自然语言查单 → F5 README → K3 复跑验证 → K4 录屏 → K5 Devpost 文案

## 五、Demo 前验收清单（当前达标情况）

| 验收项 | 状态 | 说明 |
|---|---|---|
| 全约束可满足 → 双方案正常输出 | ✅ | planner + PlansTab 已验证 |
| 预算压到 350 → 触发兜底而非硬凑 | ✅ | 方案 B 购物车 393.0 → 预算 350 超支 43.0 + 兜底建议；预算 1.0 无解给二选一（2026-11-04） |
| 自然语言改单 → 总价/分摊实时更新 | ✅ | 总价实时（E3）；分摊在确认弹窗展示（F2，2026-11-03） |
| 未点确认前 AI 无法发起支付 | ✅ | HITL 闸门 + create_order 金额强校验 |
| 支付后订单状态可自然语言查询 | ✅ | H4 `/orders/query`（服务端注入 session + 归属强制） |
| 审计日志完整记录一轮全部动作 | ✅ | I2/I3 + 9 事件埋点（含 order_status_changed） |
| K3 主线审计口径（5 步 / 12 事件） | ✅ 2026-11-04 探针 | 三条：类型覆盖 `MISSING=[]`／生命周期严格有序（review→draft→confirmed→paying→payment_completed→paid）／额外事件与操作一一对应。权威表见「K3 权威事件序列」；**不用事件条数对齐** |
| 断网/超时 → Mock 降级成功 | ✅ | Channel3 异常实测切 K1 45 件（2026-11-03 探针） |
| 启动 import-smoke（`import app.main` 失败即红灯） | ✅ | 每次提交前跑 `cd apps/server; python -c "import app.main"`（2026-11-04 加入，防 provider.py 型"启动即毁"） |


---
*维护说明：每次交付后更新本文件对应状态徽章；验收清单在录屏前必须全绿。*