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
  task.catch(() => { ensureInflight.delete(sessionId) })
  return task
}

export async function fetchConstraints(sessionId: string): Promise<ConstraintsResponse> {
  const resp = await fetch(`${API_BASE}/constraints?session_id=${encodeURIComponent(sessionId)}`)
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
  /** 检索层诚实说明（如命中全不相干 → 已回落本地目录 / 已过滤 N 件） */
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
  upgrades?: { item?: string; category?: string; from?: number; to?: number; delta?: number }[]
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

export interface PlansResponse {
  feasible: boolean
  reason?: string
  plans?: Plan[]
  plan_a?: Plan | null
  plan_b?: Plan | null
  data_source?: string
  fallback?: Record<string, unknown>
  comparison?: PlansComparison
  budget_limit?: number
  constraint_count?: number
  intent_query?: string
  intent_mode?: 'single' | 'group'
  retrieval?: RetrievalEvidence
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
  source?: 'channel3'
}

export interface RecommendationSignals {
  recent_searches?: { query: string; count?: number; ts?: string }[]
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
  const resp = await fetch(`${API_BASE}/plans?session_id=${encodeURIComponent(sessionId)}`)
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail || `Fetch plans failed: ${resp.status}`)
  }
  return await resp.json()
}

export async function selectPlan(sessionId: string, planId: 'A' | 'B'): Promise<Record<string, unknown>> {
  const resp = await fetch(
    `${API_BASE}/cart/select_plan?session_id=${encodeURIComponent(sessionId)}&plan_id=${planId}`,
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

// ===== Group purchase: order archive (H3) =====
export type OrderStatus = 'draft' | 'confirmed' | 'paying' | 'paid' | 'cancelled'

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
}

export interface OrdersResponse {
  orders: OrderSummary[]
  count: number
}

export async function fetchOrders(sessionId: string): Promise<OrdersResponse> {
  const resp = await fetch(`${API_BASE}/orders?session_id=${encodeURIComponent(sessionId)}`)
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
  const resp = await fetch(
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