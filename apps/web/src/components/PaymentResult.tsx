import { sx } from './sx'
import { Header } from './Header'
import { Footer } from './Footer'

type ResultKind = 'success' | 'cancelled' | 'returned' | 'error'

function resolveKind(pathname: string): ResultKind | null {
  if (pathname === '/payment/success') return 'success'
  if (pathname === '/payment/review') {
    const status = new URLSearchParams(window.location.search).get('status')
    if (status === 'cancelled') return 'cancelled'
    // 后端捕获/校验失败时不再把浏览器留在 500 白页，而是 302 到这里并带 status=error，
    // 于是「PayPal 登录后打不开页面」变成一张能读懂、能继续操作的失败页。
    if (status === 'error') return 'error'
    return 'returned'
  }
  return null
}

const COPY: Record<ResultKind, { title: string; desc: string; accent: string }> = {
  success: { title: '支付成功', desc: 'PayPal Sandbox 已完成批准与 capture，订单状态为 COMPLETED。', accent: '#6EE7B7' },
  cancelled: { title: '已取消支付', desc: '你在 PayPal 页面取消本次支付，未发生任何扣款。', accent: '#F5A623' },
  returned: { title: '已返回复核面板', desc: 'PayPal 流程已结束，可在下方继续确认或重新发起。', accent: '#9CA3AF' },
  error: { title: '支付未完成', desc: 'PayPal 已批准，但本地的校验/捕获这一步没走通。下方是后端返回的原因，可返回方案重试。', accent: '#F87171' },
}

/** PayPal 回跳落地页：/payment/success（成功）与 /payment/review（取消/返回）。 */
export function PaymentResult() {
  const kind = resolveKind(window.location.pathname)
  if (!kind) return null

  const params = new URLSearchParams(window.location.search)
  const orderId = params.get('order_id') || ''
  const sessionId = params.get('session_id') || ''
  const amount = params.get('amount') || ''
  const reason = params.get('reason') || ''
  const copy = COPY[kind]

  const rows: { label: string; value: string }[] = [
    { label: '订单号', value: orderId || '—' },
    { label: '金额', value: amount ? `${amount} USD` : '—' },
    { label: '会话', value: sessionId || '—' },
  ]

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', padding: '24px 16px 48px', minHeight: '100vh' }}>
      <Header />
      <div style={{ ...sx.card, borderColor: `${copy.accent}55` }}>
        <div style={{ fontSize: 12, color: copy.accent, letterSpacing: 2, textTransform: 'uppercase', fontWeight: 700 }}>
          PayPal Checkout
        </div>
        <h2 style={{ fontSize: 26, fontWeight: 800, margin: '8px 0 6px', color: copy.accent }}>{copy.title}</h2>
        <p style={{ fontSize: 14, color: '#9CA3AF', lineHeight: 1.7, margin: '0 0 18px' }}>{copy.desc}</p>

        <div style={{ borderTop: '1px solid #1F2937', paddingTop: 14 }}>
          {rows.map((row) => (
            <div key={row.label} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, padding: '7px 0', fontSize: 13 }}>
              <span style={{ color: '#6B7280' }}>{row.label}</span>
              <span style={{ color: '#E5E7EB', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', wordBreak: 'break-all' }}>
                {row.value}
              </span>
            </div>
          ))}
        </div>

        {kind === 'error' && reason && (
          <div style={{
            marginTop: 12, padding: '10px 12px', borderRadius: 8,
            background: '#111827', border: '1px solid #7F1D1D', color: '#FCA5A5',
            fontSize: 12, lineHeight: 1.6, wordBreak: 'break-word',
          }}>
            原因：{reason}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, marginTop: 20, flexWrap: 'wrap' }}>
          {kind === 'success' && (
            <button
              style={sx.btnPrimary}
              onClick={() => {
                const query = new URLSearchParams({ tab: 'orders', session_id: sessionId })
                if (orderId) query.set('order_id', orderId)
                window.location.href = `/?${query.toString()}`
              }}
            >
              查看订单档案
            </button>
          )}
          <button
            style={kind === 'success' ? sx.btnSecondary : sx.btnPrimary}
            onClick={() => { window.location.href = `/?tab=shop&session_id=${encodeURIComponent(sessionId)}` }}
          >
            返回方案继续购物
          </button>
          {kind !== 'success' && orderId && (
            <button
              style={sx.btnSecondary}
              onClick={() => { window.location.href = `https://www.sandbox.paypal.com/checkoutnow?token=${orderId}` }}
            >
              重新打开 PayPal
            </button>
          )}
        </div>
      </div>
      <Footer />
    </div>
  )
}