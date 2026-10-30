# PayPal AI Hackathon — 一页决策文档

> 目标：**品味驱动的可审计 Agentic 购物**（Agentic Shopping with Taste-grounded, Auditable Payments）
> 底座：`d:/ecv - 副本`（上一代 Agent 底座 → 复制为 `d:\ecv-paypal`）
> 差异化：`d:\aits.copy22` 的三机制（渐进授权 / 目标约束记忆 / 情绪拦截）

---

## 一、方案裁决（为什么不做那四个方案）

| 方案 | 裁决 | 原因 |
|---|---|---|
| A. VoicePay（语音转账） | ❌ | Payouts 语义不匹配"给朋友还钱"（发工资类），且网页 demo 语音默认关闭 |
| B. AgentCommerce（托管交易） | ⚠️ 只做彩蛋 | PayPal 政策禁止 escrow/资金聚合 |
| C. PaySimple | ❌ | 本质是 A + 叙事 |
| D. FreelanceFlow | 🟡 不选 | 最稳但创新最弱 |

**最终方案**：原 Agent 底座 + Orders v2 正向结账（create → 买家审批 → capture）。
**代理给到的是"买什么"的推荐 + 授权审批，资金流动永远走 PayPal 官方正轨结账，平台绝不碰资金。**

## 二、核心差异化（杀手锏）

1. **支付可解释性面板**：每一笔 agent 发起的支付都能展开看——为什么选这个商品、金额依据、收款人、匹配哪些品味锚点。这是"可审计的 agentic 支付"。
2. **渐进式授权**（搬自 aits.copy22 好感度系统）：新用户每笔都要审批 → 历史记录准确 → $20 以下自动放行。
3. **目标约束记忆**（搬自双层记忆）：记得"要存钱买相机"，冲突消费主动拦截。
4. **情绪拦截**（搬自情绪检测）：冲动消费时介入提示，不是付完才告诉。
5. **品味驱动**：Channel3 真实商品搜索 + 本地品味锚点排序，推荐真正会有共鸣的商品。

> **数据源定位裁决（2026-10-25）**：外部商品搜索是**可选增强层**，不是心脏。核心链路是
> `LLM 理解意图 → 候选商品(冻结 fixture) → 本地排序 → Orders v2 结账`，
> 完全不依赖外部 key 和 embedding 模型文件。有 Channel3 key 时实时增强排序，无 key /
> 挂掉时自动降级到本地 n-gram 排序器。→ 详见 `app/services/shopper/`。

## 二点五、Embedding 体积裁决（部署不需要模型文件）

| 组件 | 体积 | 是否进部署包 |
|---|---|---|
| `bge-reranker-base` | **3.2 GB** | ❌ 排除（95% 体积来源，核心链路用不到） |
| `faster-whisper-tiny.en` | 74 MB | ❌ 排除（STT 语音输入非演示主线） |
| `all-MiniLM-L6-v2` | 87 MB | ❌ 排除（core 排序用规则+n-gram，零模型） |
| 本地兜底排序器 `shopper/ranker.py` | **0 字节模型** | ✅ 核心链路唯一依赖（几十行纯 Python） |

- 演示/录视频：冻结 fixture `shopper/data/catalog.json`，零外部调用。
- 实时排序：`字符 n-gram 相似度 + 规则加权`（中英文都适用），无 embedding 依赖。
- `requirements-deploy.txt` 已不含 `sentence-transformers`/`faiss`，评委安装零模型开销。
- community matcher 的 embedding 保持懒加载 + 缺模型自动降级（不影响核心链路）。

## 三、三件 de-risk 验证（未验证完不写业务代码）

- [x] 1. **沙盒凭据确认**：✅ 2026-10-25 验证通过。OAuth token 获取成功，Orders v2 订单创建成功（`3U9914764C8532343`，PAYER_ACTION_REQUIRED），approval URL 生成。
- [x] 2. **Orders v2 全链路**：✅ 通道验证通过。create → approval URL ✅；capture 端点鉴权通过（无审批时返回预期的 `ORDER_NOT_APPROVED`，证明通道开放）。**待办：用沙盒 personal 账号在 approval URL 手动审批后执行真实 capture 闭环** → 已有脚本命令备用。
- [x] 3. **Payouts 试探**：✅ 2026-10-25 验证通过。HTTP 201，payout batch `V4QC69VF9AU9U` 状态 PENDING（注意字段是 `receiver` 不是 `recipient`）。为"代理间付款彩蛋"留好了后门。

> 注意：非美国集成需 Business account（官方文档要求），国内注册需先确认 Sandbox 可用。

## 四、19 天路线

| 天数 | 里程碑 |
|---|---|
| D1 | ✅ de-risk 验证 + 基建（本文件 + 新目录 + .env） |
| D2 | ✅ PayPal service 层（token 管理、orders create/capture）+ 数据模型（PaymentIntent / Approval / TrustProfile）；✅ 本地兜底排序器 + 冻结演示目录（`app/services/shopper/`，零模型文件） |
| D3 | ✅ **授权边界主线串图完成**：主图已接入支付 7 节点；`route_after_llm` 支付意图优先；`payment_approval_gate` 纳入 `interrupt_before`；AgentState 已声明支付/授权字段，支持 checkpoint 恢复；mock HITL approve/reject/allow_auto 全链路已验证 |
| D4-5 | LangGraph 新节点：品味选品 → 支付意图 → 渐进授权判定 → HITL approval_node → capture |
| D6-7 | 支付可解释性面板（后端证据拼接 + 前端 UI） |
| D8-9 | 目标约束记忆 + 情绪拦截（会话级；情绪用规则近似，不接摄像头） |
| D10-11 | 前端购物流程 + 审批体验 + 结果页 |
| D12-13 | 端到端联调 + 沙盒测试 + 幂等/失败/退款边界 |
| D14-15 | 部署（Railway/Render + Vercel）+ CORS/秘钥治理 |
| D16-17 | 3 分钟演示视频 + README + 文案 |
| D18-19 | 提交（公开 repo + LICENSE + 视频 + 描述）缓冲期 |

## 五、红线

- 平台不托管资金、不做 escrow；所有支付走 Orders v2 正向结账
- 沙盒凭据只进 `.env`（已被 .gitignore 忽略），绝不提交公开仓库
- 医学/财务不提供承诺性建议；支付前必须有人类审批（除非渐进授权已解锁）
- 诚实标注：真实 capture vs 模拟支付必须清晰区分

## 六、P2-10 收尾决策记录（2026-10-27）

### 6.1 证据长期记忆生命周期策略

- **证据面板读当期 `AgentState`**（`assemble_explainability_panel` / `evidence_groups_for_panel`
  全部取当前会话 state），**不读长期记忆**。换 session 后证据自然消失——这是有意设计：
  每笔演示订单的选品依据、授权理由、支付凭证都是"当轮审计轨迹"，不该跨会话串扰。
- **长期记忆只持久化跨会话资产**：信任画像（`payment/api.py _save_trust_profile`，
  memory key=`payment_trust_profile`，`owner="payment_agent/authz"`）带 owner 防"腐烂"；
  A/B/C 级证据明细在支付链路写入时 owner=`payment_agent`（`memory_persist_node` 检测
  paypal/shopping intent 产物注入），消除"无 owner 实体写入 → 标记腐烂"日志噪音。
- **结论**：演示证据仅 session 内有效 ✅；跨会话延续只靠渐进授权画像（分数/准确次数/阈值）。

### 6.2 七个 Output 模型的 parsed 序列化警告（已清零）

- 根因：`llm_parse_with_retry` 用 openai SDK `beta.parse` 后 `msg.model_dump()`，
  而 `ParsedChatCompletionMessage[ContentType]` 的 `parsed: Optional[~TypeVar]` 泛型字段
  在 pydantic 2.12 下序列化 schema 与实例类型不匹配 → 每个响应模型成功时各报一条
  `PydanticSerializationUnexpectedValue(Expected none [field_name='parsed'])`（共 7 类）。
- 修复：`model_dump(exclude={"parsed"})` + `msg.parsed.model_dump(mode="json")` 手动补回。
- 护栏：`tests/test_llm_parse_serialization.py`（fake beta.parse 锁定回归）。

### 6.3 支付来源标记（paypal_source 三值）

- `mock`（PAYMENT_MODE=mock）/ `sandbox_real` / `live_real`（按 PAYPAL_SANDBOX_API 判定）。
  前端 `PaymentTab` 按 source 显示来源徽标；`decision_trace.source` 与 `payment.source` 同源，
  响应层一致性断言（真实 order + mock source = 测试失败）。
- 注意：`AgentState`（TypedDict）未声明 `paypal_source` 时 langgraph 会过滤该 key ——
  必须在 `app/services/graph/state.py` 声明，否则 HTTP 响应 source 恒为空。

### 6.4 幂等边界复核

- 重复 capture：`paypal_status=="captured"` 幂等返回原回执，不重复调 PayPal ✅。
- 重复 approve：返回同一 order_id / capture_id，不重建订单/捕获 ✅
  （响应层测试 `test_duplicate_approve_is_idempotent` 覆盖）。
---

## D1 复核备注（外部结论逐条核验）

> 外部审计三条结论均正确；补两条边界注释防混淆。

1. **Pydantic parsed 序列化警告**：非 AgentState TypedDict 缺失字段导致，根因是
   OpenAI SDK ParsedChatCompletionMessage 泛型 parsed 字段直接 model_dump 触发；修复方式为
   拆分序列化，保留完整 SDK 返回实例与 last_raw，**不改动图 State 流转**。注意：这只是
   消警告，不要写成修复了 State 序列化问题。
2. **证据生命周期设计**：单轮决策证据保存在会话 State，随会话销毁；仅 TrustProfile
   信任画像跨会话持久化，绑定 owner=payment_agent，由 A/B 证据闸门控制写入，规避低置信
   推论污染信任分。答辩话术：不把每一轮完整决策 trace 灌入长期记忆，避免记忆膨胀、旧
   证据干扰新授权判定；信任分才是需要跨会话累积的核心信号。
3. **Source 一致性约束**：decision_trace.source、payment.source、llm_source 必须同源；
   测试断言拦截真实 PayPal 订单绑定 mock source 的错误状态（`test_duplicate_approve_is_idempotent`
   覆盖重复 approve 幂等；capture 幂等分支由 hardening 测试锁定）。

> 潜在坑：前端面板不要默认把历史信任分变更记录当成本次决策证据。本次面板只展示
> 本次 authority_check 的打分拆解与 LLM 理由；信任画像单独放小卡片，作为计算输入因子。
