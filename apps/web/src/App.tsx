import { useState, useCallback, useEffect } from 'react'
import { ShopTab } from './components/ShopTab'
import { ConstraintsDashboard } from './components/tabs/ConstraintsDashboard'
import { AuditTimeline } from './components/tabs/AuditTimeline'
import { OrdersTab } from './components/tabs/OrdersTab'
import { Header } from './components/Header'
import { Footer } from './components/Footer'
import { ToastProvider } from './components/ToastProvider'
import { PaymentResult } from './components/PaymentResult'
import { AgentComposer } from './components/AgentComposer'
import { usePaymentAgent } from './hooks/usePaymentAgent'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ensureSession, selectPlan, submitIntent } from './api'
import { useToast } from './hooks/useToast'

/**
 * 兼容旧深链：老版本把「方案对比」和「对话与支付」拆成两个面板，
 * 合二为一后就地支付，所以 plans / chat 一律落到 shop。
 */
function initialPanel(): 'shop' | 'audit' | 'orders' {
  const tab = new URLSearchParams(window.location.search).get('tab')
  return tab === 'audit' || tab === 'orders' ? tab : 'shop'
}

/** 回跳 CTA 携带的 order_id：订单档案面板据此自动展开该订单（含已支付订单）。 */
function initialOrderId(): string | undefined {
  const orderId = new URLSearchParams(window.location.search).get('order_id')
  return orderId || undefined
}

/** Main application shell: 搜索即推荐（拼多多式） + 就地支付 + 两条留痕面板 */
function AppShell() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const payment = usePaymentAgent({ setLoading, setError })
  const { sessionId } = payment
  const [activePanel, setActivePanel] = useState<'shop' | 'audit' | 'orders'>(initialPanel)
  const [intentQuery, setIntentQuery] = useState<string | null>(null)
  const [intentMode, setIntentMode] = useState<'single' | 'group' | null>(null)
  const [composerBusy, setComposerBusy] = useState(false)
  const [planRefreshKey, setPlanRefreshKey] = useState(0)

  // 刷新保持当前页面：tab 写进 URL（深链可分享），滚动位置按 tab 记在 sessionStorage。
  useEffect(() => {
    const url = new URL(window.location.href)
    url.searchParams.set('tab', activePanel)
    url.searchParams.set('session_id', sessionId)
    if (intentQuery) url.searchParams.set('q', intentQuery)
    window.history.replaceState(null, '', url)
  }, [activePanel, sessionId, intentQuery])

  useEffect(() => {
    const key = `ecv.scroll.${activePanel}`
    const saved = Number(sessionStorage.getItem(key) || '0')
    // 切 tab 时恢复该 tab 上次的滚动位置（刷新后也保持）
    requestAnimationFrame(() => window.scrollTo({ top: saved }))
    return () => {
      try { sessionStorage.setItem(key, String(window.scrollY)) } catch { /* quota */ }
    }
  }, [activePanel])

  const handleComposerSubmit = useCallback(async (text: string) => {
    if (!sessionId) return
    setComposerBusy(true)
    setError('')
    // 搜索即推荐：提交后停在同一个页面（不用跳任何 Tab），下拉直接看方案
    setIntentQuery(text)
    setActivePanel('shop')
    // 新需求 = 新方案：旧的"已选择"必须清掉，否则新方案一出来就显示"已选择"
    setSelectedPlanId(null)
    try {
      const res = await submitIntent(sessionId, text)
      setIntentMode(res.mode)
      setPlanRefreshKey(k => k + 1)
      // 约束看板随写随显：通知它立即重拉，不等 3s 轮询
      window.dispatchEvent(new CustomEvent('ecv:constraints-changed'))
      toast(
        `🔎 检索关键词「${res.query}」· ${res.mode === 'group' ? '团购' : '单人'}模式 · 新增 ${res.extracted_count} 条约束`,
        'success',
      )
    } catch (e) {
      setError(String(e))
      toast(`需求提交失败：${String(e)}`, 'error')
    } finally {
      setComposerBusy(false)
    }
  }, [sessionId, toast])

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
        {/* Pinduoduo 式「上面是搜索框，下面出推荐」：吸顶提问框全局常驻 */}
        <AgentComposer
          onSubmit={handleComposerSubmit}
          busy={composerBusy}
          lastQuery={intentQuery}
          lastMode={intentMode}
        />

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
            ['shop', '🛒 选购方案 · 就地确认'],
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

        {/* 合二为一：方案对比 + 对话/支付 同页纵向排布（搜索→推荐→就地支付） */}
        {activePanel === 'shop' && (
          <ShopTab
            sessionId={sessionId}
            onSelectPlan={handleSelectPlan}
            selectedPlanId={selectedPlanId}
            refreshKey={planRefreshKey}
            intentQuery={intentQuery}
            intentMode={intentMode}
          />
        )}

        {/* 审计时间轴：决策链「为什么」；订单档案：状态机「到哪一步」，两者互补 */}
        {activePanel === 'audit' && <AuditTimeline sessionId={sessionId} />}

        {/* 订单档案（Agent 授权演示订单不在此列，见 server） */}
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