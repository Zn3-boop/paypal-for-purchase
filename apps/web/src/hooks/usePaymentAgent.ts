import { useCallback, useEffect, useRef, useState } from 'react'
import {
  paymentAgentChat,
  paymentApprove,
  paymentSetGoal,
  type PaymentAgentResponse,
  type PaymentGoalResponse,
} from '../api'
import { useToast } from './useToast'

export type PaymentChatMsg = {
  id: string
  role: 'user' | 'agent' | 'approval' | 'system'
  text: string
  /** Payment agent structured payload (from /payment/agent and /payment/approve). */
  res?: PaymentAgentResponse
}

const SESSION_KEY = 'payment.session.v1'
const CHAT_KEY = 'payment.chat.v1'

/** Stable session id per browser (survives reloads so trust can compound). */
function loadSessionId(): string {
  try {
    const existing = localStorage.getItem(SESSION_KEY)
    if (existing) return existing
    const id = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    localStorage.setItem(SESSION_KEY, id)
    return id
  } catch {
    return `web-${Date.now()}`
  }
}

function loadChat(): PaymentChatMsg[] {
  try {
    const raw = localStorage.getItem(CHAT_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as PaymentChatMsg[]
    if (!Array.isArray(parsed)) return []
    return parsed.filter(m => m && typeof m.id === 'string' && typeof m.text === 'string').slice(-60)
  } catch { return [] }
}

type Busy = { setLoading: (v: boolean) => void; setError: (e: string) => void }

export function usePaymentAgent({ setLoading, setError }: Busy) {
  const toast = useToast()
  const [sessionId] = useState<string>(loadSessionId)
  const [chatMsg, setChatMsg] = useState('')
  const [history, setHistory] = useState<PaymentChatMsg[]>(loadChat)
  const endRef = useRef<HTMLDivElement>(null)
  const [pending, setPending] = useState<PaymentAgentResponse | null>(null)
  const [goals, setGoals] = useState<PaymentGoalResponse['goal'][]>([])
  const [goalText, setGoalText] = useState('')
  const [goalSaving, setGoalSaving] = useState(false)

  useEffect(() => { try { localStorage.setItem(CHAT_KEY, JSON.stringify(history.slice(-60))) } catch { /* quota */ } }, [history])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [history])

  const push = useCallback((m: PaymentChatMsg) => setHistory(h => [...h, m]), [])
  const pushAgent = useCallback((res: PaymentAgentResponse) => push({ id: crypto.randomUUID(), role: 'agent', text: '', res }), [push])

  const doChat = useCallback(async () => {
    const text = chatMsg.trim()
    if (!text) return
    setChatMsg(''); setError('')
    setLoading(true)
    push({ id: crypto.randomUUID(), role: 'user', text })
    try {
      const res = await paymentAgentChat({ message: text, sessionId })
      pushAgent(res)
      setPending(res.needs_approval ? res : null)
    } catch (e) { setError(String(e)) }
    setLoading(false)
  }, [chatMsg, setLoading, setError, push, pushAgent, sessionId])

  const decide = useCallback(async (decision: 'approve' | 'reject' | 'modify') => {
    if (!pending) return
    setError(''); setLoading(true)
    push({ id: crypto.randomUUID(), role: 'approval', text: '' }) // renders previous pending card as decided
    try {
      const res = await paymentApprove({ sessionId, userId: 'demo-user', decision })
      pushAgent(res)
      setPending(null)
      toast(decision === 'approve' ? '✅ 已批准' : decision === 'reject' ? '❌ 已否决' : '✏️ 已修改后放行', 'success')
    } catch (e) {
      setError(String(e))
      toast(`审批提交失败: ${String(e)}`, 'error')
    }
    setLoading(false)
  }, [pending, sessionId, push, pushAgent, setLoading, setError, toast])

  const setGoal = useCallback(async () => {
    const text = goalText.trim()
    if (!text) return
    setError(''); setGoalSaving(true)
    push({ id: crypto.randomUUID(), role: 'system', text: `🎯 储蓄目标：${text}` })
    try {
      const res = await paymentSetGoal({ userId: 'demo-user', sessionId, text })
      setGoalText('')
      setGoals(res.goal ? [res.goal] : res.active_goals ?? [])
      push({ id: crypto.randomUUID(), role: 'system', text: `已保存：${res.message}` })
      toast('🎯 目标已设置（后续享受型消费会被拦截确认）', 'success')
    } catch (e) {
      setError(String(e))
      toast(`目标设置失败: ${String(e)}`, 'error')
    }
    setGoalSaving(false)
  }, [goalText, push, sessionId, setError, setGoalSaving, toast])

  const clear = useCallback(() => {
    setHistory([]); setGoals([])
    try { localStorage.removeItem(CHAT_KEY) } catch { /* noop */ }
  }, [])

  return { sessionId, chatMsg, setChatMsg, history, setHistory, clear, endRef, doChat, decide, pending, setPending, goals, setGoal, goalText, setGoalText, goalSaving }
}