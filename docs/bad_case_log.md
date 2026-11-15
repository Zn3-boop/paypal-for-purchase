# Bad Case 复盘总账

> 对齐「定位 20+ Bad Case 并迭代优化」的量化证据。每条 = 一个**真实发生过、
> 被修复、且留下回归测试**的缺陷。证据锚点是 commit，不是回忆。

口径：
- **现象**：用户/演示/测试里看到的外部表现。
- **根因**：判定链路哪一环错了（检索/归一化/判定/规划/支付/会话）。
- **修复**：提交里实际改了什么。
- **防回归**：对应的回归测试或门禁（`pytest` 用例 / `quality_gate` / golden）。

## 总览（22 项）

| # | 分类 | Bad Case 一句话 | 修复 commit |
|---|------|-----------------|------------|
| 1 | 归一化 | 商品 `materials`/`structured_attributes` 未并入 tags，材质/颜色/版本/容量四类约束「查得到却证明不了」 | `f921e98` |
| 2 | 判定 | 「加大款」被抽取层切成「加大」，STYLE_KEYWORDS 缺 large 变体 → 漏判 | `faf203d` |
| 3 | 判定 | 鞋/皮鞋实体缺词表，皮鞋类需求召回判 ❔ | `68c1849` |
| 4 | 判定 | 「哑光」无反证：商品自述亮面时应判 unknown/sat❌，旧实现只看名字 | `68c1849` |
| 5 | 判定 | 容量数值语义：「500ml」「500 毫升」两种写法不同 → 判定分叉 | `68c1849` |
| 6 | 判定 | 快充词表缺失，充电器「快充」约束被判 ❔ | `68c1849` |
| 7 | 判定 | `description` 不作证据源，颜色/材料/规格词典不全，规格变体不生成 → 约束频繁 ❔ | `8eed766` |
| 8 | 检索 | 缓存命中绕过熔断的时序根因（团购 error vs 单人 ok 的假象） | `5a772a6` |
| 9 | 检索 | 熔断期内无恢复路径，Channel3 不可用时整条链路长时间降级 | `2fcd7ea`（半开探针） |
| 10 | 规划 | 预算口径错：单件预算被当总价门槛、同款×N 装配缺失、无解不诊断 | `e2a67cb` |
| 11 | 基线工具 | quality_gate 假阳性 unsat：原脚本拿原始 20 件直接判，混入超预算伞 | `ee3067d`（prefilter 修复） |
| 12 | 基线工具 | 规格数字被判定层丢弃（「316 不锈钢」→ 永远 ❔） | `ee3067d`（_extract_specs 上下文修复） |
| 13 | 存储 | 会话无 TTL/无持久化，刷新即丢 | `fcee135`（PG 双写 + TTL） |
| 14 | 会话 | 孤儿 session 不收敛 → 整屏 404 风暴 | `dee2db2` |
| 15 | 方案 | B 方案被讲成「推荐」，实际是独立重排 → 口径撒谎 | `86ecc44` |
| 16 | 推荐 | 推荐卡无排除原因，干涉「为什么没有方案里的件」 | `86ecc44` |
| 17 | 支付 | 前端可伪造 amount，捕获金额 ≠ 订单金额未拦截 | `e9e7562`（callback 守卫） |
| 18 | 支付 | PayPal 建单失败无回滚，状态机卡死在半途 | `e9e7562` |
| 19 | 支付 | 检索与支付之间存在改价/缺货窗口，无兜底 | `96229fe`（P1-1 offer 重校验） |
| 20 | 配置 | 区域配置写了但没透传到 SDK，商家外链被吞，免责声明缺失 | `855a4d8`（P0 收口） |
| 21 | 依赖声明 | `pg_store` 顶层 import psycopg 但 requirements.txt 没声明 → CI 收集阶段炸（exit 2） | `6467b17` |
| 22 | 测试环境前提 | 6 个测试依赖本地 `.env` 偶然凭据才绿：paypal 2 个真实 sandbox 联调误入默认套件、channel3 3 个无 key 被 disabled 短路、删单指令走 LLM 分支缺 key 构造即抛 → CI 干净环境全红（exit 1） | `46961ba` |

## 详情（节选：每个类挑一个典型展开）

### 归一化层 —— materials/structured_attributes 并入 tags（#1）

- 现象：用户要「316 不锈钢保温杯」，检索能找回商品，判定却 sat 不了——材料在
  Channel3 返回的 `materials`/`structured_attributes` 字段里，判定层只读 `tags`。
- 根因：归一化层丢弃了结构化属性，`_query_tokens` 算相关度时这些证据不可见。
- 修复（`f921e98`）：归一化层把 `materials`/`structured_attributes` 并回 `tags`，
  四类属性约束（材质/颜色/版型/容量）从此「查得到就能证明」。
- 防回归：`pytest` 归一化相关用例覆盖「属性并回后约束可 sat」。

### 判定层 —— 「加大款」词法切分边界（#2）

- 现象：需求「加大款」应命中 `large`，「加大款」被判定交付前切词成「加大」，
  `STYLE_KEYWORDS` 的 key 是 `加大款`，字面不匹配 → 漏判 PLAN_B。
- 根因：抽取层把「加大款」按更细粒度切分，判定层按原 key 匹配，口径不一致。
- 修复（`faf203d`）：`STYLE_KEYWORDS` 补 `large` 变体 key，两种切分都覆盖。
- 防回归：变体用例进 `test_satisfaction_hardening.py`。

### 判定层 —— 材质反证（#4）

- 现象：用户要「哑光」，候选商品自述「亮面/漆皮」，旧实现不看反证 → 误判 sat。
- 根因：判定只看正向词命中，不做「自证反证」的硬约束。
- 修复（`68c1849`）：反证字面命中时降级判定，审计里留证据（`evidence` 写反证原文）。
- 防回归：哑光反证用例进 `test_satisfaction_hardening.py`，`quality_gate` 包装材类场景。

### 检索韧性 —— 缓存命中绕过熔断（#8）

- 现象：看起来「团购 error 而单人 ok」的矛盾现象。
- 根因：熔断标记以单个 key 计，缓存命中路径绕过了熔断检查——根因是时序而非网络。
- 修复（`5a772a6`）：先把时序根因钉死成回归测试，再修熔断判断路径。
- 防回归：`test_cart_items_api.py` 相关用例。

### 支付 —— 检索与支付之间的合法窗口（#19）

- 现象：检索到支付之间商家改价/缺货，用户以旧价下单。
- 根因：`offer_url` 保留但无人回源复核。
- 修复（`96229fe`）：`create_order` 前按 `offer_url` 实时回源，单件涨幅 >10%、
  整篮 >5%、缺货/下架均拦截；回源失败默认 fail-closed（`FAIL_OPEN` 显式放行）。
- 防回归：`test_offer_recheck.py` 14 用例（断言 400 + 审计事件 + `gateway.calls`）。

### 依赖声明 —— psycopg 缺失引爆 CI collection error（#21）

- 现象：CI 红，`pytest exited with code 2`；本地 Windows 459 全绿。不是测试失败，
  是 `pytest` 收集阶段就崩（一个测试都没跑起来）。
- 根因：`pg_store.py` 顶层 `from psycopg import connect`，但 `psycopg` 不在
  `requirements.txt`。本地环境恰好装有 3.3.9 → 绿；CI 全新 pip 环境没有 → 炸。
  更讽刺的是 requirements 第 64 行留着注释「PostgreSQL 驱动已移除，项目使用
  SQLite」——但 `pg_store.py`（PG 双写）和 `test_pg_store.py` 22 个用例都在用。
- 修复（`6467b17`）：① `requirements.txt` 补 `psycopg[binary]>=3.2,<4.0`，删过时
  注释；② `pg_store.py` 防御性 import（psycopg 缺失时模块可导入、函数优雅降级
  no-op，与 `audit_log` 旁路约定一致）；③ `test_pg_store.py` 模块级
  `pytest.importorskip("psycopg")`——缺失时整体 skip 而非 fail。
- 防回归：用 meta_path 拦截器模拟「psycopg 不存在」跑全量 → 437 passed + 1 模块
  skip（`test_pg_store` 整体跳过），**不再收集阶段崩**；正常环境 459 passed。
- 教训：**docstring 里写的契约必须在 import 层就兑现**。`pg_store.py` 第一行写着
  「DATABASE_URL 未设置时所有函数都是 no-op」——但 psycopg 缺失时模块 import 就崩，
  这个 no-op 契约根本没机会生效。「可选依赖」要成立必须三层齐备：**模块能导入、
  功能能降级、测试能跳过**。少任何一件，它就不是可选依赖，是隐藏的硬依赖。

### 测试环境前提 —— 6 个凭据依赖测试在 CI 红（#22）

- 现象：CI exit 1，6 个测试失败（453 个通过）：`ValueError: Missing PAYPAL
  credentials`（2 个）、`status='disabled', 未配置 CHANNEL3_API_KEY`（3 个）、
  `openai.OpenAIError: Missing credentials`（1 个）。本地 Windows 全绿（459）——
  全部是「本地 `.env` 有真实凭据、CI 没有」的偶然差异。
- 根因：三类各自「没声明测试前提」：
  ① paypal 2 个是**真实 sandbox 联调**（真凭据 + 真调 `api-m.sandbox.paypal.com`），
     文件头 `load_dotenv()` 读 `.env`，却没有任何 e2e 标记 —— `pytest.ini` 早有
     `addopts = -m "not e2e"` 机制，它们被默认离线套件误收集；
  ② channel3 resilience 3 个调**模块级函数** `search_products_with_status` →
     `get_channel3_client()` 从 Settings 读 key，CI 无 key → `enabled=False` →
     原实现 `not self.enabled` 在「熔断/探针」逻辑**之前**就返回 disabled，
     patch 的 `_search_with_trust_chain` 根本不会被调用，测试走不到验证对象；
  ③ `test_delete_instruction_still_works` 走 LLM 分支（「删掉 HD 300」不是
     「只要 X」句式）→ `get_llm_client()` 构造 `AsyncOpenAI(api_key='')` →
     openai 新版 `_enforce_credentials` 直接抛，LLM 失败后的规则兜底根本没机会执行。
- 修复（`46961ba`）：
  ① paypal 2 个标 `@pytest.mark.e2e`（默认 `-m "not e2e"` 排除；需真凭据时
     `python -m pytest -m e2e` 显式激活）；
  ② channel3 autouse fixture 显式 `monkeypatch.setenv("CHANNEL3_API_KEY",
     "test-key")` + 重置模块单例（`get_settings` 默认 cache=False 每次全新读 env，
     假 key 让 `enabled=True`，熔断/探针路径真正可达）；
  ③ `test_delete_instruction_still_works` 把 `llm_parse_with_retry` mock 成不可用
     （返回 `(None, raw)`）→ 验证「LLM 失败时规则兜底仍能删掉 HD 300」——这正是
     该回归的本意，且不再依赖 `.env`。
- 防回归：**无 .env 全量跑**（临时 rename `.env` 模拟 CI 干净环境）→ 457 passed +
  2 deselected，与有 `.env` 的结果完全一致。口径：459 collected = 457 默认套件 +
  2 e2e（`-m e2e` 显式激活）。
- 教训：**测试的绿色不能来自「恰好 `.env` 有值」**。要么测试自给自足（显式注入
  假凭据 / mock 依赖层），要么标记成 e2e 移出默认套件——CI 是提交合同，离线套件
  必须在全新裸环境（无凭据、无网络）全绿。第 7 类管「import 层声明」，第 8 类管
  「测试环境前提声明」，同一句原则：**契约必须兑现，不是看本地 `.env` 脸色**。

## 迭代闭环（怎么证明「迭代过」）

1. 每个 Bad Case 先**钉死为回归测试**（复现 → 红 → 修复 → 绿）。
2. `quality_gate`（30 句模板需求 × 真实检索 × 三态判定）做离线基线；
3. `satisfaction_golden.py`（7 个对拍场景）防现场漂移；
4. `scripts/retrieval_ablation.py`（检索/重排策略消融）证明重排环节的选择有量化依据，
   而不是「拍脑袋加了个 MMR」。

## 错误模式归因（8 类）——面试时这样讲

复盘不是流水账。「我修了 22 个 bug」是流水账；「我把 22 个问题归成 8 类错误模式，
每一类对应一个判定层的设计约束」是工程思维。以下 8 类是项目全部 Bad Case 的归因：

| # | 错误模式 | 表现 | 判定层设计约束 | 典型证据（commit） |
|---|---------|------|--------------|-------------------|
| 1 | 假装满足 | 判 ✅ 但实际不满足 | 判定必须有**反证检查**（自证亮面则不能判哑光） | `68c1849`、`ee3067d`（prefilter） |
| 2 | 假装不满足 | 商品明明满足，系统说判不了 ❔ | 证据源必须完整（description/materials/词表） | `8eed766`、`faf203d`、`f921e98` |
| 3 | 数字不可复算 | 件数/总价/份额对不上 | 金额类约束必须有**作用域**（total vs per_item） | `e2a67cb` |
| 4 | 说了不存在的原因 | 给出的「建议」与真实原因无关 | 诊断信息必须来自真实判定路径 | `e2a67cb`（无解诊断） |
| 5 | 显示与判定不一致 | 同一事实两个说法 | 前端渲染只消费判定层的同一份 verdicts | `86ecc44`、`87aa2b8` |
| 6 | 语义层级错位 | 把组合语义套到单件/把噪音当约束 | 约束建模区分组合层与逐件层 | `cb221b4`（规则原子化） |
| 7 | 声明与实现不一致 | 文档/测试假设「可选依赖」，import 层却当硬依赖 | 可选依赖必须三层齐备：模块能导入、功能能降级、测试能跳过 | `6467b17` |
| 8 | 测试环境前提未声明 | 本地 `.env` 偶然凭据让它绿，CI 干净环境红；live 联调要真凭据却混在离线套件 | 单元测试必须自给自足（显式注入假凭据 / mock 依赖层）；live 联调必须标 e2e 进默认排除 | `46961ba` |

## 五字段复盘模板（每类可展开成一个完整案例）

以「单件预算被当总预算」为例（第 3 类错误模式）：

### BC-06 单件预算被当成总预算

- **现象**：用户说「单件预算不超 160」，系统判「总预算 ≤160，$432.40 超支 $272.40」——
  但每一件都 ≤160，用户看到的是「明明符合却判我不符合」。
- **根因**：约束模型缺 `scope` 字段。抽取层只存了 `numeric_value=160`，
  丢失了「这个 160 是作用于单件还是整个组合」的语义。
- **修法**：`Constraint` 加 `scope: total | per_item`，判定层分两个分支；
  per_item 分支遍历每件取最大单价比较（`total_budget_cap`）。
- **验证**：探针「单件预算≤130 + 12 件键盘」→ 全部 12 件单价 ≤ $130.00（最高 $99.00）。
- **教训**：**约束不只是「阈值」，还有「作用域」**。任何金额类约束，抽取时都必须
  回答「这个数是对谁说的」。缺了作用域，判定层就只能猜——而猜错的方向恰好是
  「更严格」，用户会觉得系统在骗他。