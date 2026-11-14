// The backend already prefixes business APIs with /api/v1 (/ and /health are liveness paths, no version).
export const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '') + '/api/v1'

/**
 * FastAPI 的 detail 既可能是字符串，也可能是结构化对象（例如预算守卫拦截返回
 * {status, message, overage, ...}）。直接塞进 Error 会显示成 "[object Object]"，
 * 这里统一取出可读文案。
 */
function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    const obj = detail as Record<string, unknown>
    const msg = obj.message ?? obj.detail ?? obj.reason
    if (typeof msg === 'string' && msg.trim()) return msg
    try { return JSON.stringify(detail) } catch { return fallback }
  }
  return fallback
}
// ===== Payment authorization flow (LangGraph main graph) =====
export interface PaymentAgentResponse {
  reply: string
  needs_approval: boolean
  status: string
  payment: {
    item?: { name?: string; price?: number; currency?: string; category?: string; image_url?: string; description?: string }
    rank_source?: string
    mode?: string
    order_id?: string
    capture_id?: string
    status?: string
    approval_url?: string
    msg?: string
    error?: string
    source?: string
    intent_id?: string
    paypal_order_id?: string
    business_order_id?: string
    evidence_caveats?: string[]
  }
  authority: {
    verdict?: string
    rule?: string
    reason?: string
    llm_reason?: string
    risk_level?: string
    trust_score?: number
    auto_threshold?: number
    goal_conflicts?: string[]
    impulse_signals?: string[]
    llm_source?: string
  }
  approval: { status?: string; comment?: string; evidence_applied?: Record<string, unknown> }
  explain: { text?: string; llm_source?: string; structured?: Record<string, unknown>; trust_updated?: Record<string, unknown> }
  evidence: { title: string; kind: string; rows: { label: string; value: string }[] }[]
  llm_source?: string
  session_id: string
  trace_id: string
}

export async function paymentAgentChat(input: {
  message: string
  sessionId: string
  userId?: string
  monthlySpent?: number
}): Promise<PaymentAgentResponse> {
  const resp = await fetch(`${API_BASE}/payment/agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: input.message,
      session_id: input.sessionId,
      user_id: input.userId || 'demo-user',
      monthly_spent: input.monthlySpent || 0,
    }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Payment agent failed: ${resp.status}`)
  }
  return await resp.json()
}

export type PaymentGoalResponse = {
  ok: boolean
  goal?: { target_label: string; target_amount: number; monthly_spend_cap: number; since: string; active: boolean } | null
  active_goals: { target_label: string; target_amount: number; monthly_spend_cap: number; since: string; active: boolean }[]
  message: string
}

export async function paymentSetGoal(input: {
  userId?: string
  sessionId: string
  text: string
  monthlySpendCap?: number
}): Promise<PaymentGoalResponse> {
  const resp = await fetch(`${API_BASE}/payment/goal`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: input.userId || 'demo-user',
      session_id: input.sessionId,
      text: input.text,
      monthly_spend_cap: input.monthlySpendCap ?? 300,
    }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Payment goal failed: ${resp.status}`)
  }
  return await resp.json()
}

export async function paymentApprove(input: {
  sessionId: string
  userId?: string
  decision: 'approve' | 'reject' | 'modify'
  comment?: string
}): Promise<PaymentAgentResponse> {
  const resp = await fetch(`${API_BASE}/payment/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: input.sessionId,
      user_id: input.userId || 'demo-user',
      decision: input.decision,
      comment: input.comment || '',
    }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Payment approval failed: ${resp.status}`)
  }
  return await resp.json()
}

// ===== Group purchase: constraints =====
export interface ConstraintItem {
  id?: string
  type: string
  content?: string | null
  numeric_value?: number | null
  hard?: boolean
  status?: string
  category?: string | null
  source?: string
}

export interface ConflictItem {
  type?: string
  detail?: string
  [key: string]: unknown
}

export interface ConstraintsResponse {
  session_id?: string
  constraints: ConstraintItem[]
  conflicts?: ConflictItem[]
  [key: string]: unknown
}

/**
 * 确保后端存在该团购 session。
 *
 * 事故背景（2026-11-04）：前端只在 localStorage 生成 `web-<ts>-<rand>` 并永久复用，
 * 从不调用创建接口；团购 session 是后端**进程内存**，后端一重启旧 id 就成孤儿，
 * 约束/方案/购物车/订单全 404（储蓄目标不受影响是因为 GoalMemory 按 user 另存）。
 *
 * 规则：先查 `GET /session/{id}/status`，仅当 `exists === false` 才用
 * `POST /session/reset` materialize —— 绝不 reset 已存在的会话，绝不误清已有状态。
 * 同一页面生命周期内只执行一次（in-flight 去重，StrictMode 双挂载也安全）。
 */
const ensureInflight = new Map<string, Promise<void>>()

/**
 * 会话自举：没有就建，有就复用。
 *
 * 注意这里**只判断 exists，不判断属主**。清理残留不是前端的职责 ——
 * 会话有没有"过期"由后端读路径惰性判定（session_state.is_session_expired，
 * 滑动窗口 last_active_at + SESSION_TTL_SECONDS）。过期后 status 直接返回
 * exists=false，这里自然走到下面新建分支，前端不需要知道 TTL 的存在。
 */
export function ensureSession(sessionId: string, userId = 'demo-user'): Promise<void> {
  if (!sessionId) return Promise.resolve()
  const cached = ensureInflight.get(sessionId)
  if (cached) return cached

  const task = (async () => {
    const statusResp = await fetch(`${API_BASE}/session/${encodeURIComponent(sessionId)}/status`)
    if (statusResp.ok) {
      const status = await statusResp.json().catch(() => null)
      if (status?.exists) return
    }
    const resp = await fetch(`${API_BASE}/session/reset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, user_id: userId }),
    })
    if (!resp.ok) throw new Error(`Session bootstrap failed: ${resp.status}`)
  })()

  ensureInflight.set(sessionId, task)
  // 只用来合并"同一时刻的并发调用"，任务结束就清掉。
  // 早期版本在成功后永久保留，导致后端重启、session 变成孤儿时这里一直返回"已建好"的旧结论，
  // 面板轮询会永远 404（这就是 11-09 那次整屏 "Session not found" 的根因）。
  task.then(
    () => ensureInflight.delete(sessionId),
    () => ensureInflight.delete(sessionId),
  )
  return task
}

/**
 * 后端重启会让 localStorage 里的 session id 变成孤儿：接口返回 404 + detail "Session not found"，
 * 而面板每 3~5 秒轮询一次，于是整屏刷 404。这里识别这个特定 404，重建一次会话后重试，
 * 让"开着页面重启后端"自愈，而不是要求用户手动刷新。
 */
function isSessionMissing(detail: unknown): boolean {
  return typeof detail === 'string' && detail.trim() === 'Session not found'
}

async function fetchWithTimeout(url: string, timeoutMs?: number): Promise<Response> {
  if (!timeoutMs) return fetch(url)
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    return await fetch(url, { signal: ctrl.signal })
  } finally {
    clearTimeout(timer)
  }
}

async function withSessionRetry(sessionId: string, url: string, timeoutMs?: number): Promise<Response> {
  const first = await fetchWithTimeout(url, timeoutMs)
  if (first.status !== 404) return first
  const body = await first.clone().json().catch(() => null)
  if (!isSessionMissing((body as { detail?: unknown } | null)?.detail)) return first
  await ensureSession(sessionId)
  return await fetchWithTimeout(url, timeoutMs)
}

export async function fetchConstraints(sessionId: string): Promise<ConstraintsResponse> {
  const resp = await withSessionRetry(
    sessionId,
    `${API_BASE}/constraints?session_id=${encodeURIComponent(sessionId)}`,
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch constraints failed: ${resp.status}`)
  }
  return await resp.json()
}

export interface ExtractConstraintsResponse {
  ok: boolean
  extracted_count: number
  added_ids?: string[]
  constraints?: ConstraintItem[]
  conflicts?: ConflictItem[]
  message?: string
}

/** A5：按已添加/系统约束 ID 软删除；删除型自然语言在前端只做匹配，不经过 LLM extract。 */
export async function deleteConstraint(sessionId: string, constraintId: string): Promise<{ ok: boolean; deleted_id: string }> {
  const resp = await fetch(`${API_BASE}/constraints/${encodeURIComponent(constraintId)}?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Delete constraint failed: ${resp.status}`)
  }
  return await resp.json()
}
export async function extractConstraints(sessionId: string, text: string): Promise<ExtractConstraintsResponse> {
  const resp = await fetch(`${API_BASE}/constraints/extract?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, source: 'user' }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Extract constraints failed: ${resp.status}`)
  }
  return await resp.json()
}

// ===== Group purchase: plans =====
export interface PlanItem {
  id: string
  name: string
  category?: string
  price: number
  quantity: number
  subtotal?: number
  matched_constraints?: string[]
  source?: string
  rationale?: string
  /** Channel3 证据：商品图、标签、关键词匹配分（本地 catalog 时为空）。 */
  image_url?: string
  tags?: string[]
  match_score?: number
  match_tags?: string[]
}

/** 赞助商 Channel3 检索到、但「不完全匹配」本次需求的商品（回落本地时单独展示）。 */
export interface SponsorRelatedItem {
  id: string
  name: string
  price: number
  currency?: string
  category?: string
  image_url?: string
  tags?: string[]
}

/** 检索证据：「推荐依据」的数据源。query 是用户原句，search_query 是实际发给 Channel3 的检索词。 */
export interface RetrievalEvidence {
  query?: string
  search_query?: string
  source?: string
  candidate_count?: number
  matched_count?: number
  /**
   * 过滤数（候选 − 命中，后端强制守恒，可心算核对）。
   * 2026-11-14 起口径单一来源：candidate=原始召回条数 / matched=约束预筛后 / filtered=candidate−matched。
   */
  filtered_count?: number
  /** 检索层诚实说明（如命中全不相干 → 已回落本地目录），不含数字口径。 */
  note?: string
  /**
   * Channel3 三态（进字段，不只进文案）：
   *   ok=命中 / empty=检索成功但商品库无匹配 / error=检索暂时不可用（可重试） / disabled=未启用
   */
  channel3_status?: 'ok' | 'empty' | 'error' | 'disabled' | ''
  /** Channel3（赞助商）最接近但不完全匹配的商品：绝不混进方案，单独成卡。 */
  sponsor_related?: SponsorRelatedItem[]
  sponsor_note?: string
}

/** 预算 Guardrail 检查结果（对齐后端 budget_guardrail.check_budget）。 */
export interface BudgetCheck {
  limit: number
  total: number
  overage: number
  budget?: number
  resolution?: string
  suggestions?: {
    original_item?: string
    replacement?: string
    original_price?: number
    replacement_price?: number
    savings?: number
    reason?: string
  }[]
}

export interface Plan {
  plan_id: 'A' | 'B'
  strategy?: string
  items: PlanItem[]
  total: number
  currency?: string
  matched_constraints?: string[]
  unmet_constraints?: string[]
  trade_offs?: string[]
  risks?: string[]
  source?: string
  budget_ok?: boolean
  budget_check?: BudgetCheck
  sacrificed_members?: string[]
  /** 约束满足度总账：组合级 N/M 条 + 逐条 ✅/❌/❔ + 证据串（satisfaction.py 纯计算）。 */
  satisfaction?: Satisfaction
  /** 无明确预算时「最贴题」超软上限（最省方案 × 3）：前端折叠到「超预算」区，不冒充推荐。 */
  soft_cap_exceeded?: boolean
  soft_cap_note?: string
  /** 数量诚实（评审盯点②）：需求 N 件但候选池只凑得出 M 件（M<N）时置位 —— 不冒充「需求已满足」。 */
  feasible?: boolean
  quantity_shortfall?: boolean
  quantity_note?: string
}

/** 逐条约束的判定：sat=满足 / unsat=不满足 / unknown=数据判不了（绝不假装 ✅）。 */
export type VerdictStatus = 'sat' | 'unsat' | 'unknown'
export type ItemVerdictStatus = 'pass' | 'fail' | 'n/a'

export interface SatisfactionVerdict {
  constraint_id: string
  type: string
  content: string
  source?: string
  hard?: boolean
  status: VerdictStatus
  /** 可复算的证据串，如 "$285.00 ≤ $350.00" / "标签命中环保（recycled）"。 */
  evidence?: string | null
  reason?: string | null
  /** false = 超过 ❌ 显示上限被折叠（数据仍在，只是不展示）。 */
  visible?: boolean
}

export interface SatisfactionItemRow {
  item_id: string
  name: string
  price: number
  passed: number
  failed: number
  results: {
    constraint_id: string
    label: string
    type: string
    status: ItemVerdictStatus
    evidence: string
  }[]
}

export interface Satisfaction {
  judgeable: boolean
  total: number
  passed: number
  failed: number
  unknown: number
  headline: string
  max_failures_shown: number
  hidden_failures: string[]
  constraints: SatisfactionVerdict[]
  items: SatisfactionItemRow[]
}

/** 后端 planner._compare_plans() 返回的双方案对比摘要。 */
export interface PlansComparison {
  total_delta?: number
  item_count_a?: number
  item_count_b?: number
  shared_count?: number
  only_a_count?: number
  only_b_count?: number
  budget_limit?: number
  recommendation?: string
  upgrades?: {
    item?: string
    to_item?: string
    to_id?: string
    category?: string
    from?: number
    to?: number
    delta?: number
    /** 替代分级：T0 同一商品 / T1 同款异规格 / T2 同品牌同类型 / T3 同品牌 */
    tier?: number | null
    tier_label?: string | null
  }[]
  /** 「凭什么这样比」：同一候选池 + 双目标（最低总价 / 最高关键词匹配分）。 */
  criteria?: ComparisonCriteria
  metrics?: ComparisonMetrics
  pareto?: ComparisonPareto
}

export interface ComparisonCriteria {
  domain?: string
  objective_a?: string
  objective_b?: string
  same_feasibility?: string
  evidence?: string
  /** B 的替换上限（最多几件、什么条件）。 */
  upgrade_scope?: string
  /** 替代分级硬门（tier ≤ T3 / 单件 ≤ 50% / 整篮 ≤ 30%）：可复算的「凭什么能换」。 */
  substitution_gate?: string
  /** B 的选品方式：substitution（A 的同款替换）/ relevance_reselect（无可辩护替换 → 同池重排）。 */
  selection_mode_b?: 'independent' | 'substitution' | 'relevance_reselect' | string
}

export interface ComparisonMetrics {
  total_a?: number
  total_b?: number
  relevance_a?: number
  relevance_b?: number
  item_count_a?: number
  item_count_b?: number
  shared_count?: number
}

/** 帕累托判定：trade_off = 真实取舍；a/b_dominates = 另一方案被支配，必须当场说明。 */
export interface ComparisonPareto {
  relation?: 'trade_off' | 'a_dominates' | 'b_dominates'
  note?: string
  total_a?: number
  total_b?: number
  relevance_a?: number
  relevance_b?: number
}

/** 无可行方案时的说明与建议（后端 generate_fallback_message 生成）。 */
export interface PlansFallbackInfo {
  ok?: boolean
  status?: string
  message?: string
  reason?: string
  suggestions?: Array<{ action?: string; detail?: string; estimated_impact?: string }>
  [key: string]: unknown
}

export interface PlansResponse {
  feasible: boolean
  reason?: string
  plans?: Plan[]
  plan_a?: Plan | null
  plan_b?: Plan | null
  data_source?: string
  fallback?: PlansFallbackInfo
  comparison?: PlansComparison
  budget_limit?: number
  constraint_count?: number
  intent_query?: string
  intent_mode?: 'single' | 'group'
  retrieval?: RetrievalEvidence
  /** 无解时的约束诊断（satisfaction_scope='catalog'：在候选池全量上判的覆盖情况）。 */
  satisfaction?: Satisfaction
  satisfaction_scope?: 'catalog' | string
  [key: string]: unknown
}

/** 统一入口：同一个 Agent 提问框同时服务单人/团购。返回本次检索关键词与更模式。 */
export interface IntentResponse {
  ok: boolean
  query: string
  mode: 'single' | 'group'
  extracted_count: number
  added_ids?: string[]
  /** 被本次提问替换掉的上一次自动约束 id（手动添加的约束不受影响）。 */
  replaced_count?: number
  removed_ids?: string[]
  constraints_count?: number
  message?: string
}

/** Channel3 检索记忆：去重后的最近检索原句（count = 同一句被搜过几次）。 */
export interface SearchMemoryEntry {
  ts?: string
  session_id?: string
  user_id?: string
  query: string
  mode?: 'single' | 'group'
  count?: number
}

/** 选择记忆：最近选过的方案（含总额 / 件数 / 当时的检索原句）。 */
export interface SelectionMemoryEntry {
  ts?: string
  session_id?: string
  plan_id?: 'A' | 'B'
  total?: number
  item_count?: number
  items?: string[]
  query?: string
}

export interface SearchMemoryResponse {
  recent_searches: SearchMemoryEntry[]
  recent_selections: SelectionMemoryEntry[]
}

/** 只读的团队级检索记忆（本地演示无多租户隔离）。失败不抛给 UI 主流程。 */
export async function fetchSearchMemory(limit = 8): Promise<SearchMemoryResponse> {
  const resp = await fetch(`${API_BASE}/search-memory?limit=${limit}`)
  if (!resp.ok) throw new Error(`search-memory failed: ${resp.status}`)
  return resp.json()
}

/**
 * Channel3 推荐卡：读侧推导的「第三条泳道」，与 A/B 方案平行、不混入对比。
 * 每张卡都带出处（reason / basis / query_used / search_hits），评委可复算。
 */
export interface RecommendationCard {
  id: string
  name: string
  category?: string
  price: number
  currency?: string
  image_url?: string
  tags?: string[]
  match_score?: number
  match_tags?: string[]
  reason: string
  basis?: 'search' | 'selection' | 'anchor' | 'none'
  basis_value?: string
  /** 主原因码（一卡一主码）：FILLS_CATEGORY_GAP / CHEAPER_ALTERNATIVE / SAME_RELEASE_OTHER_EDITION / SAME_ARTIST / SAME_GENRE / HISTORY_DIRECTION */
  reason_code?: string
  /** 至多一个辅码（只做补充，不改主叙事）。 */
  reason_code_secondary?: string | null
  /** 主码对应的可复核人话（纯函数产出，不依赖模型随机性）。 */
  reason_code_text?: string
  /** 与本次检索的归一化相关度 [0,1]（rel = min(1, 命中锚点/4)）。 */
  relevance?: number
  /** 与上方 A/B 的关系：in_a / in_b / in_both / neither（neither 必带 placement_reason）。 */
  placement?: 'in_a' | 'in_b' | 'in_both' | 'neither'
  placement_reason?: string
  source?: 'channel3'
}

export interface RecommendationSignals {
  recent_searches?: { query: string; count?: number; ts?: string; session_id?: string }[]
  last_selection?: {
    plan_id?: string
    total?: number
    item_count?: number
    items?: string[]
    query?: string
    ts?: string
  } | null
  selected_items?: string[]
  anchor_texts?: string[]
}

export interface RecommendationsResponse {
  items: RecommendationCard[]
  source: 'channel3' | 'none'
  query_used: string
  display_query: string
  search_hits: number
  shown: number
  signals: RecommendationSignals
  channel3: { status: string; message?: string }
  note?: string
}

export interface AddRecommendationResponse {
  success: boolean
  code?: string
  message?: string
  item?: { id?: string; name?: string; price?: number }
  cart?: { items: unknown[]; total: number }
  guardrail?: {
    status: 'ok' | 'fail' | 'blocked' | 'no_budget'
    ok: boolean
    budget: number | null
    total: number
    overage: number | null
    message: string
  }
}

export async function fetchRecommendations(
  sessionId: string,
  limit = 3,
): Promise<RecommendationsResponse> {
  const resp = await fetch(
    `${API_BASE}/recommendations?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
  )
  if (!resp.ok) throw new Error(`recommendations failed: ${resp.status}`)
  return resp.json()
}

/** 加购走后端预算守卫：超硬预算默认 blocked 且购物车不变（force 才放行）。 */
export async function addRecommendation(
  sessionId: string,
  itemId: string,
  force = false,
): Promise<AddRecommendationResponse> {
  const resp = await fetch(`${API_BASE}/recommendations/add`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, item_id: itemId, force }),
  })
  if (!resp.ok) throw new Error(`add recommendation failed: ${resp.status}`)
  return resp.json()
}

export async function submitIntent(sessionId: string, message: string): Promise<IntentResponse> {
  const resp = await fetch(
    `${API_BASE}/plans/intent?session_id=${encodeURIComponent(sessionId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, source: 'agent_composer' }),
    },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Intent submit failed: ${resp.status}`)
  }
  return await resp.json()
}

/** 显式重算方案（快照唯一主动失效入口）：改完约束 / 换口味后调它，再 fetchPlans 拿新方案。 */
export async function regeneratePlans(sessionId: string): Promise<{ ok: boolean; query: string; constraints_count: number }> {
  const resp = await fetch(
    `${API_BASE}/plans/regenerate?session_id=${encodeURIComponent(sessionId)}`,
    { method: 'POST' },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Regenerate plans failed: ${resp.status}`)
  }
  return await resp.json()
}

export async function fetchPlans(sessionId: string): Promise<PlansResponse> {
  // 超时兜底：后端生成最坏约 8s（Channel3 硬超时）+ 本地目录，25s 覆盖重试余量；
  // 超时抛错 → PlansTab 轮询会按「网络抖动」节奏继续，不会无限白屏。
  const resp = await withSessionRetry(
    sessionId,
    `${API_BASE}/plans?session_id=${encodeURIComponent(sessionId)}`,
    25000,
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch plans failed: ${resp.status}`)
  }
  return await resp.json()
}

export async function selectPlan(
  sessionId: string,
  planId: 'A' | 'B',
  mode: 'replace' | 'append' = 'replace',
): Promise<Record<string, unknown>> {
  const resp = await fetch(
    `${API_BASE}/cart/select_plan?session_id=${encodeURIComponent(sessionId)}&plan_id=${planId}&mode=${mode}`,
    { method: 'POST' },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Select plan failed: ${resp.status}`)
  }
  return await resp.json()
}


// ===== Group purchase: split preview =====
export interface SplitMemberPreview {
  member_id: string
  name: string
  amount: number
  is_adjustment: boolean
}

export interface SplitPreviewResponse {
  total: number
  per_person: number
  splits: SplitMemberPreview[]
  adjustment_member: string | null
}

// ===== Group purchase: cart =====

/**
 * 当前购物车（服务端唯一真相）。
 *
 * 下单金额由 `POST /paypal/create_order` 按购物车重算，前端传的 amount 只作日志对照；
 * 所以弹窗打开时必须以这里为准 —— 选方案会把购物车重置成方案快照，推荐泳道的
 * 「加入购物车」是在此基础上追加，两者叠加后的真实金额只有 /cart 知道。
 */
export interface CartResponse {
  items: PlanItem[]
  total: number
  /** 预算合规事实（只报告不拦截）；服务端 GET /cart 与 PATCH/DELETE 都会带上。 */
  guardrail?: CartGuardrail
}

export async function fetchCart(sessionId: string): Promise<CartResponse> {
  const resp = await withSessionRetry(
    sessionId,
    `${API_BASE}/cart?session_id=${encodeURIComponent(sessionId)}`,
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch cart failed: ${resp.status}`)
  }
  return await resp.json()
}

export async function fetchSplitPreview(sessionId: string): Promise<SplitPreviewResponse> {
  const resp = await fetch(
    `${API_BASE}/cart/split_preview?session_id=${encodeURIComponent(sessionId)}`,
    { method: 'POST' },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch split preview failed: ${resp.status}`)
  }
  return await resp.json()
}

// ===== Group purchase: cart 可编辑闭环（PATCH 改数量 / DELETE 删行） =====

/** 购物车合规事实：**只报告不拦截** —— 用户要能「改到预算内再付」，把关点在 create_order。 */
export interface CartGuardrail {
  status: 'ok' | 'fail' | 'no_budget'
  ok: boolean
  budget: number | null
  total: number
  overage: number | null
  message?: string
}

export interface CartMutationResponse {
  success: boolean
  message?: string
  item_id?: string
  quantity?: number
  old_total?: number
  new_total?: number
  cart: CartResponse
  guardrail?: CartGuardrail
}

export async function addCartItem(sessionId: string, itemId: string): Promise<CartMutationResponse> {
  const resp = await fetch(
    `${API_BASE}/cart/items/${encodeURIComponent(itemId)}?session_id=${encodeURIComponent(sessionId)}`,
    { method: 'POST' },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(detailMessage(body?.detail, `Add cart item failed: ${resp.status}`))
  }
  return await resp.json()
}

export async function updateCartItemQuantity(
  sessionId: string,
  itemId: string,
  quantity: number,
): Promise<CartMutationResponse> {
  const resp = await fetch(
    `${API_BASE}/cart/items/${encodeURIComponent(itemId)}?session_id=${encodeURIComponent(sessionId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ quantity }),
    },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(detailMessage(body?.detail, `Update quantity failed: ${resp.status}`))
  }
  return await resp.json()
}

export async function removeCartItem(sessionId: string, itemId: string): Promise<CartMutationResponse> {
  const resp = await fetch(
    `${API_BASE}/cart/items/${encodeURIComponent(itemId)}?session_id=${encodeURIComponent(sessionId)}`,
    { method: 'DELETE' },
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(detailMessage(body?.detail, `Remove item failed: ${resp.status}`))
  }
  return await resp.json()
}

// ===== Group purchase: order archive (H3) =====
export type OrderStatus = 'draft' | 'confirmed' | 'paying' | 'paid' | 'cancelled' | 'expired'

export interface OrderSummary {
  order_id: string
  session_id: string
  status: OrderStatus
  total: number
  currency: string
  plan_id: string | null
  created_at: string
  updated_at: string
  cancelled_reason?: string | null
  /** paying 的支付窗口截止时间（ISO8601）；超时后读接口会惰性迁移到 expired。 */
  expires_at?: string | null
}

export interface OrdersResponse {
  orders: OrderSummary[]
  count: number
}

export async function fetchOrders(sessionId: string): Promise<OrdersResponse> {
  const resp = await withSessionRetry(
    sessionId,
    `${API_BASE}/orders?session_id=${encodeURIComponent(sessionId)}`,
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch orders failed: ${resp.status}`)
  }
  return await resp.json()
}

/** 订单详情：状态机轨迹 + 商品快照 + 关联审计事件（复用 AuditTimeline 的 EVENT_META 渲染）。 */
export interface OrderTransition {
  from_status?: string | null
  to_status?: string
  at?: string
  actor?: string
  reason?: string | null
  [key: string]: unknown
}

export interface OrderItemSnapshot {
  name?: string
  qty?: number
  quantity?: number
  price?: number
  subtotal?: number
  [key: string]: unknown
}

export interface OrderDetail extends OrderSummary {
  user_id?: string
  paypal_token?: string | null
  items_count?: number
  items?: OrderItemSnapshot[]
  transitions?: OrderTransition[]
  audit_events?: AuditLogEntry[]
  audit_count?: number
  audit_error?: string
}

export async function fetchOrderDetail(orderId: string, sessionId: string): Promise<OrderDetail> {
  const resp = await fetch(
    `${API_BASE}/orders/${encodeURIComponent(orderId)}?session_id=${encodeURIComponent(sessionId)}`,
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch order detail failed: ${resp.status}`)
  }
  return await resp.json()
}

/** H4 自然语言查单（只读）。session_id 由前端会话注入，后端强制归属校验。 */
export interface OrderQueryResponse {
  ok: boolean
  reason?: string
  ambiguous?: boolean
  intent?: string
  order_id?: string
  status?: OrderStatus
  status_label?: string
  total?: number
  currency?: string
  plan_id?: string | null
  updated_at?: string
  transitions?: number
  reply: string
  orders: OrderSummary[]
}

export async function queryOrder(sessionId: string, text: string): Promise<OrderQueryResponse> {
  const resp = await fetch(`${API_BASE}/orders/query?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Query order failed: ${resp.status}`)
  }
  return await resp.json()
}

// ===== Group purchase: audit logs =====
export interface AuditLogEntry {
  timestamp: string
  round_num: number
  event: string
  session_id: string
  user_id: string
  actor: string
  data?: Record<string, unknown>
  constraint_snapshot?: unknown[] | null
  plan_snapshot?: unknown[] | null
}

export interface AuditLogsResponse {
  session_id: string
  logs: AuditLogEntry[]
  total: number
}

export async function fetchAuditLogs(sessionId: string, limit = 50): Promise<AuditLogsResponse> {
  const resp = await withSessionRetry(
    sessionId,
    `${API_BASE}/audit/logs?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
  )
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch audit logs failed: ${resp.status}`)
  }
  return await resp.json()
}

export interface CreateOrderRequest {
  amount: number
  session_id: string
  user_email: string
  item_name: string
}

export interface CreateOrderResponse {
  order_id: string
  approval_url: string
  status: string
  idempotent?: boolean
}

export async function createOrder(req: CreateOrderRequest): Promise<CreateOrderResponse> {
  const resp = await fetch(`${API_BASE}/paypal/create_order`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      amount: req.amount,
      session_id: req.session_id,
      user_email: req.user_email,
      item_name: req.item_name,
    }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(detailMessage(body?.detail, `Create order failed: ${resp.status}`))
  }
  return await resp.json()
}

// ===== Group purchase: cart modification（自然语言改单，供「预算拦截后改单重试」）=====
export interface CartModifyResponse {
  success: boolean
  message?: string
  diff?: { changed_items?: unknown[]; old_total?: number; new_total?: number }
  guardrail?: { status?: string; message?: string | null }
  cart?: { items?: unknown[]; total?: number }
  fallback?: unknown
  split_preview?: unknown
  intent_summary?: unknown
  warnings?: string[]
}

export async function modifyCart(sessionId: string, instruction: string): Promise<CartModifyResponse> {
  const resp = await fetch(`${API_BASE}/cart/modify?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction }),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(detailMessage(body?.detail, `Modify cart failed: ${resp.status}`))
  }
  return await resp.json()
}