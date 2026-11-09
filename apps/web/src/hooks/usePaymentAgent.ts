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

/**
 * Stable session id per browser (survives reloads so trust can compound).
 *
 * `?fresh=1` 强制开新会话（清掉旧 id 与聊天），是彩排/录制的隔离开关：
 * 彩排时点过的加购会写进那个 session 的审计流水，用同一个 id 录制就会多出一条
 * 观众没看见的事件，而"13 个事件"是口播的核心数字。彩排一个 id，录制另一个。
 */
function loadSessionId(): string {
  try {
    const params = new URLSearchParams(window.location.search)
    if (params.get('fresh') === '1') {
      localStorage.removeItem(SESSION_KEY)
      localStorage.removeItem(CHAT_KEY)
      params.delete('fresh')
      const qs = params.toString()
      window.history.replaceState(null, '', `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`)
    }
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

  // PayPal 整页回跳后恢复支付对话上下文；仅展示后端回调返回的状态。
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const status = params.get('payment_status')
    if (!status) return
    push({
      id: crypto.randomUUID(),
      role: 'system',
      text: status === 'captured'
        ? '✅ PayPal 买家已批准，订单捕获成功。'
        : `⚠️ PayPal 已返回，但支付未完成（${status || '未知状态'}）。`,
    })
    params.delete('payment_status')
    params.delete('paypal_token')
    params.set('tab', 'chat')
    window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`)
  }, [push])

  const doChat = useCallback(async (overrideText?: string) => {
    // overrideText：嵌入式「确认支付」按钮用一句话直接发起，不依赖顶部输入框
    // （单页只有一个文字入口，支付区不再重复放 🎤/Send）。
    const text = (overrideText ?? chatMsg).trim()
    if (!text) return
    setChatMsg(''); setError('')
    setLoading(true)
    push({ id: crypto.randomUUID(), role: 'user', text })
    try {
      const res = await paymentAgentChat({ message: text, sessionId })
      pushAgent(res)
      setPending(res.needs_approval ? res : null)
      if (res.payment?.status === 'created' && res.payment.approval_url) {
        window.location.assign(res.payment.approval_url)
      }
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
      if (res.payment?.capture_id) {
        toast(decision === 'approve' ? '✅ 支付完成' : decision === 'reject' ? '❌ 订单已取消' : '📝 修改后放行', 'success')
      } else if (res.payment?.approval_url) {
        window.location.assign(res.payment.approval_url)
      } else {
        toast(decision === 'approve' ? '✅ 已批准' : decision === 'reject' ? '❌ 已否决' : '✏️ 已修改后放行', 'success')
      }
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