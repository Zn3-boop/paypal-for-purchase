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
    evidence_caveats?: string[]
  }
  authority: {
    verdict?: string
    rule?: string
    reason?: string
    llm_reason?: string
    risk_level?: string
    trust_score?: number
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
  budget_limit?: number
  recommendation?: string
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
  [key: string]: unknown
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