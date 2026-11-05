import { useState, useCallback, useEffect } from 'react'
import { PaymentTab } from './components/tabs/PaymentTab'
import { ConstraintsDashboard } from './components/tabs/ConstraintsDashboard'
import { PlansTab } from './components/tabs/PlansTab'
import { AuditTimeline } from './components/tabs/AuditTimeline'
import { OrdersTab } from './components/tabs/OrdersTab'
import { Header } from './components/Header'
import { Footer } from './components/Footer'
import { ToastProvider } from './components/ToastProvider'
import { PaymentResult } from './components/PaymentResult'
import { usePaymentAgent } from './hooks/usePaymentAgent'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ensureSession, selectPlan } from './api'

/** 支持 PayPal 回跳后深链到指定面板（如 /?tab=orders&order_id=...）。 */
function initialPanel(): 'plans' | 'chat' | 'audit' | 'orders' {
  const tab = new URLSearchParams(window.location.search).get('tab')
  return tab === 'chat' || tab === 'audit' || tab === 'orders' ? tab : 'plans'
}

/** 回跳 CTA 携带的 order_id：订单档案面板据此自动展开该订单（含已支付订单）。 */
function initialOrderId(): string | undefined {
  const orderId = new URLSearchParams(window.location.search).get('order_id')
  return orderId || undefined
}

/** Main application shell with four-panel layout */
function AppShell() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const payment = usePaymentAgent({ setLoading, setError })
  const { sessionId } = payment
  const [activePanel, setActivePanel] = useState<'plans' | 'chat' | 'audit' | 'orders'>(initialPanel)
  const [selectedPlanId, setSelectedPlanId] = useState<'A' | 'B' | null>(null)
  const [sessionReady, setSessionReady] = useState(false)

  // 后端重启会让 localStorage 里的旧 session id 变成孤儿（全站 404 "Session not found"）。
  // 挂载时先确保会话存在（exists=false 才创建，不会误清已有会话），再挂载依赖它的面板，
  // 避免首屏打出一串 404（这才是 2026-11-04 404 风暴的根因）。
  useEffect(() => {
    let alive = true
    ensureSession(sessionId, 'demo-user')
      .catch(err => console.warn('Session bootstrap failed:', err))
      .finally(() => { if (alive) setSessionReady(true) })
    return () => { alive = false }
  }, [sessionId])

  const handleSelectPlan = useCallback(async (planId: 'A' | 'B') => {
    await selectPlan(sessionId, planId)
    setSelectedPlanId(planId)
    // 不切面板：选方案后紧接着在 PlansTab 内打开二次确认弹窗
  }, [sessionId])

  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '24px 16px 48px', minHeight: '100vh' }}>
      <ErrorBoundary>
        <Header />

        {!sessionReady ? (
          <div style={{ padding: 24, textAlign: 'center', color: '#9CA3AF', fontSize: 13 }}>
            正在初始化会话...
          </div>
        ) : (
          <>
        {/* Panel 1: Constraints Dashboard */}
        <div style={{ marginBottom: 16 }}>
          <ConstraintsDashboard sessionId={sessionId} />
        </div>

        {/* Panel tabs */}
        <div style={{ display: 'flex', gap: 4, marginBottom: 12, borderBottom: '1px solid #374151' }}>
          {([
            ['plans', '🗺️ 方案对比'],
            ['chat', '💬 对话与支付'],
            ['audit', '📜 审计时间轴'],
            ['orders', '📦 订单档案'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setActivePanel(key)}
              style={{
                padding: '8px 14px',
                background: 'none',
                border: 'none',
                borderBottom: activePanel === key ? '2px solid #3B82F6' : '2px solid transparent',
                color: activePanel === key ? '#E5E7EB' : '#9CA3AF',
                fontWeight: activePanel === key ? 700 : 500,
                fontSize: 13,
                cursor: 'pointer',
              }}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Panel 2: Plans comparison */}
        {activePanel === 'plans' && (
          <PlansTab sessionId={sessionId} onSelectPlan={handleSelectPlan} selectedPlanId={selectedPlanId} />
        )}

        {/* Panel 3: Chat / payment */}
        {activePanel === 'chat' && (
          <PaymentTab
            chatMsg={payment.chatMsg}
            setChatMsg={payment.setChatMsg}
            history={payment.history}
            setHistory={payment.setHistory}
            endRef={payment.endRef}
            doChat={() => void payment.doChat()}
            decide={payment.decide}
            pending={payment.pending}
            loading={loading}
            onClear={payment.clear}
            goals={payment.goals}
            goalText={payment.goalText}
            setGoalText={payment.setGoalText}
            setGoal={() => void payment.setGoal()}
            goalSaving={payment.goalSaving}
          />
        )}

        {/* Panel 4: Audit timeline */}
        {activePanel === 'audit' && <AuditTimeline sessionId={sessionId} />}

        {/* Panel 5: Group purchase order archive (Agent-authorization demo orders excluded) */}
        {activePanel === 'orders' && <OrdersTab sessionId={sessionId} initialOrderId={initialOrderId()} />}

        {error && <div role="alert" style={{ color: '#FCA5A5', marginTop: 12, fontSize: 12 }}>{error}</div>}
          </>
        )}
      </ErrorBoundary>
      <Footer />
    </div>
  )
}

/** PayPal callback landing page shares the same entry point. */
export default function App() {
  if (window.location.pathname.startsWith('/payment/')) {
    return <PaymentResult />
  }
  return (
    <ToastProvider>
      <AppShell />
    </ToastProvider>
  )
}