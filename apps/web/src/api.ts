// The backend already prefixes business APIs with /api/v1 (/ and /health are liveness paths, no version).
export const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '') + '/api/v1'
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
