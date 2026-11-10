import { useEffect, useState } from 'react'
import {
  fetchOrderDetail,
  fetchOrders,
  queryOrder,
  type OrderDetail,
  type OrderQueryResponse,
  type OrderSummary,
  type OrderStatus,
} from '../../api'
import { EVENT_META } from './auditEventMeta'
import { EventDataSummary } from './auditEventSummary'

type Props = {
  sessionId: string
  /** PayPal 回跳 CTA 携带的 order_id：进面板即自动展开该订单（含已支付订单）。 */
  initialOrderId?: string
}

const STATUS_LABEL: Record<OrderStatus, string> = {
  draft: '待复核', confirmed: '已确认', paying: '支付中', paid: '已支付', cancelled: '已取消', expired: '已超时',
}
const STATUS_COLOR: Record<OrderStatus, string> = {
  draft: '#FBBF24', confirmed: '#60A5FA', paying: '#C084FC', paid: '#34D399', cancelled: '#F87171', expired: '#94A3B8',
}
const fmtUsd = (amount: number, currency: string) => `${currency === 'USD' ? '$' : ''}${amount.toFixed(2)}${currency === 'USD' ? '' : ` ${currency}`}`

export function OrdersTab({ sessionId, initialOrderId }: Props) {
  const [orders, setOrders] = useState<OrderSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [polling, setPolling] = useState(false)
  const [listVersion, setListVersion] = useState(0)
  const [expandedId, setExpandedId] = useState<string | null>(initialOrderId || null)
  const [detail, setDetail] = useState<OrderDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [queryText, setQueryText] = useState('')
  const [queryBusy, setQueryBusy] = useState(false)
  const [queryResult, setQueryResult] = useState<OrderQueryResponse | null>(null)
  const [queryError, setQueryError] = useState<string | null>(null)

  // 列表：首屏必拉一次；仅当存在 paying 订单时按 4s 续期，全部终结后自动停表。
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    let timer: number | undefined

    const tick = async () => {
      try {
        const res = await fetchOrders(sessionId)
        if (!alive) return
        const next = res.orders || []
        setOrders(next)
        setError(null)
        setListVersion(v => v + 1)
        const hasPaying = next.some(order => order.status === 'paying')
        setPolling(hasPaying)
        if (hasPaying) timer = window.setTimeout(() => { void tick() }, 4000)
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : '订单加载失败')
        setPolling(false)
      } finally {
        if (alive) setLoading(false)
      }
    }

    void tick()
    return () => {
      alive = false
      if (timer) window.clearTimeout(timer)
    }
  }, [sessionId])

  // 详情：展开即拉；轮询期间随 listVersion 刷新。loading 由 detail/error 的空态推导，避免 effect 内同步 setState。
  useEffect(() => {
    if (!sessionId || !expandedId) return
    let alive = true
    fetchOrderDetail(expandedId, sessionId)
      .then(res => { if (alive) setDetail(res) })
      .catch(err => {
        if (!alive) return
        setDetail(null)
        setDetailError(err instanceof Error ? err.message : '订单详情加载失败')
      })
    return () => { alive = false }
  }, [sessionId, expandedId, listVersion])

  /** H4：只读查单；命中唯一订单时顺带展开详情，不修改任何状态机。 */
  const handleQuery = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || queryBusy) return
    setQueryBusy(true)
    setQueryError(null)
    try {
      const res = await queryOrder(sessionId, trimmed)
      setQueryResult(res)
      if (res.ok && !res.ambiguous && res.order_id) {
        setExpandedId(res.order_id)
        setDetail(null)
        setDetailError(null)
      }
    } catch (err) {
      setQueryResult(null)
      setQueryError(err instanceof Error ? err.message : '查单失败，请稍后再试')
    } finally {
      setQueryBusy(false)
    }
  }

  return (
    <section style={{ background: '#0f172a', borderRadius: 12, padding: 16, border: '1px solid #374151' }}>
      <div style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <h3 style={{ margin: 0, fontSize: 14, color: '#E5E7EB' }}>📦 订单档案</h3>
          {polling && <span style={{ fontSize: 10, color: '#C084FC' }}>● 支付中 · 每 4s 自动刷新</span>}
        </div>
        <div style={{ fontSize: 11, color: '#9CA3AF', marginTop: 5 }}>
          仅展示团购 OrderRecord；“Agent 授权边界”演示订单不入档。
        </div>
      </div>

      {/* H4：自然语言查单（只读）。快捷句降低演示时的手输成本。 */}
      <form
        onSubmit={e => { e.preventDefault(); void handleQuery(queryText) }}
        style={{ display: 'flex', gap: 6, marginBottom: 8 }}
      >
        <input
          value={queryText}
          onChange={e => setQueryText(e.target.value)}
          placeholder="问一句：我的订单到哪一步了？"
          aria-label="自然语言查单"
          style={{ flex: 1, background: '#111827', border: '1px solid #374151', borderRadius: 6, padding: '7px 9px', color: '#E5E7EB', fontSize: 12 }}
        />
        <button
          type="submit"
          disabled={queryBusy || !queryText.trim()}
          style={{ padding: '7px 12px', borderRadius: 6, border: '1px solid #3B82F6', background: queryBusy ? '#1f2937' : '#1e3a8a', color: '#DBEAFE', fontSize: 12, fontWeight: 700, cursor: queryBusy ? 'default' : 'pointer' }}
        >
          {queryBusy ? '查询中…' : '查订单'}
        </button>
      </form>
      <div style={{ display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap' }}>
        {['我的订单到哪一步了？', '最近一笔订单状态', '订单付款了吗？'].map(chip => (
          <button
            key={chip}
            type="button"
            onClick={() => { setQueryText(chip); void handleQuery(chip) }}
            disabled={queryBusy}
            style={{ padding: '3px 8px', borderRadius: 99, border: '1px solid #374151', background: '#111827', color: '#9CA3AF', fontSize: 10, cursor: queryBusy ? 'default' : 'pointer' }}
          >
            {chip}
          </button>
        ))}
      </div>
      {queryError && <div style={{ color: '#FCA5A5', fontSize: 11, marginBottom: 10 }}>⚠️ {queryError}</div>}
      {queryResult && (
        <div style={{
          marginBottom: 10, padding: 9, borderRadius: 6, fontSize: 11, lineHeight: 1.6, whiteSpace: 'pre-wrap',
          background: queryResult.ok ? '#0b1f16' : '#1f1a0b',
          border: `1px solid ${queryResult.ok ? '#34D39955' : '#F59E0B55'}`,
          color: queryResult.ok ? '#D1FAE5' : '#FDE68A',
        }}>
          {queryResult.reply}
        </div>
      )}

      {loading && <div style={{ color: '#9CA3AF', fontSize: 12 }}>加载订单中…</div>}
      {error && <div style={{ color: '#FCA5A5', fontSize: 12 }}>⚠️ {error}</div>}
      {!loading && !error && orders.length === 0 && (
        <div style={{ color: '#9CA3AF', fontSize: 12, padding: '16px 0' }}>当前会话暂无团购订单。</div>
      )}
      <div style={{ display: 'grid', gap: 8 }}>
        {orders.map(order => {
          const open = expandedId === order.order_id
          const currentDetail = detail?.order_id === order.order_id ? detail : null
          return (
            <div key={order.order_id}>
              <OrderRow
                order={order}
                open={open}
                onToggle={() => {
                  if (!open) {
                    setDetail(null)
                    setDetailError(null)
                  }
                  setExpandedId(open ? null : order.order_id)
                }}
              />
              {open && (
                <OrderDetailPanel
                  detail={currentDetail}
                  fallback={order}
                  loading={!currentDetail && !detailError}
                  error={detailError}
                />
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

function OrderRow({ order, open, onToggle }: { order: OrderSummary; open: boolean; onToggle: () => void }) {
  return (
    <div style={{ background: '#1e293b', borderRadius: 8, padding: 12, border: `1px solid ${open ? '#3B82F6' : '#374151'}` }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          style={{ background: 'none', border: 'none', padding: 0, textAlign: 'left', cursor: 'pointer', flex: 1 }}
        >
          <div style={{ color: '#E5E7EB', fontSize: 13, fontWeight: 700 }}>
            {open ? '▾' : '▸'} {order.order_id}
          </div>
          <div style={{ color: '#9CA3AF', fontSize: 11, marginTop: 3 }}>
            方案 {order.plan_id || '—'} · {new Date(order.created_at).toLocaleString()}
          </div>
        </button>
        <div style={{ textAlign: 'right' }}>
          <div style={{ color: '#E5E7EB', fontWeight: 700, fontSize: 14 }}>{fmtUsd(order.total, order.currency)}</div>
          <span style={{ color: STATUS_COLOR[order.status], fontSize: 11, fontWeight: 700 }}>{STATUS_LABEL[order.status]}</span>
        </div>
      </div>
      {order.cancelled_reason && <div style={{ color: '#FCA5A5', fontSize: 11, marginTop: 8 }}>原因：{order.cancelled_reason}</div>}
    </div>
  )
}

const sectionTitle = (text: string) => (
  <div style={{ fontSize: 11, fontWeight: 700, color: '#9CA3AF', letterSpacing: 0.5, margin: '10px 0 6px' }}>{text}</div>
)

/** 详情：复用审计时间轴的 EVENT_META / EventDataSummary，保证同一事件在两处口径一致。 */
function OrderDetailPanel({ detail, fallback, loading, error }: {
  detail: OrderDetail | null
  fallback: OrderSummary
  loading: boolean
  error: string | null
}) {
  const source = detail ?? fallback
  const items = detail?.items ?? []
  const transitions = detail?.transitions ?? []
  const auditEvents = detail?.audit_events ?? []

  return (
    <div style={{ marginTop: 6, background: '#111827', border: '1px solid #374151', borderRadius: 8, padding: 12 }}>
      {loading && !detail && <div style={{ color: '#9CA3AF', fontSize: 11 }}>加载订单详情…</div>}
      {error && <div style={{ color: '#FCA5A5', fontSize: 11 }}>⚠️ {error}（下方为列表摘要）</div>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 8, fontSize: 11 }}>
        <Meta label="状态" value={STATUS_LABEL[source.status]} color={STATUS_COLOR[source.status]} />
        <Meta label="金额" value={fmtUsd(source.total, source.currency)} />
        <Meta label="方案" value={source.plan_id || '—'} />
        <Meta label="商品件数" value={String(detail?.items_count ?? items.length ?? '—')} />
        <Meta label="会话" value={source.session_id} mono />
      </div>

      {sectionTitle('状态机轨迹')}
      {transitions.length === 0 ? (
        <div style={{ fontSize: 11, color: '#6B7280' }}>暂无状态转移记录。</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {transitions.map((t, idx) => (
            <div key={idx} style={{ display: 'flex', gap: 6, alignItems: 'baseline', fontSize: 11, color: '#D1D5DB', flexWrap: 'wrap' }}>
              <span style={{ color: '#60A5FA' }}>{String(t.from_status || '·')} → {String(t.to_status || '·')}</span>
              <span style={{ color: '#6B7280' }}>{(t.at || '').slice(11, 19)}</span>
              {t.actor && <span style={{ color: '#9CA3AF' }}>@{String(t.actor)}</span>}
              {t.reason && <span style={{ color: '#FCA5A5' }}>{String(t.reason)}</span>}
            </div>
          ))}
        </div>
      )}

      {sectionTitle('商品快照')}
      {items.length === 0 ? (
        <div style={{ fontSize: 11, color: '#6B7280' }}>暂无商品快照。</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {items.map((item, idx) => {
            const qty = Number(item.qty ?? item.quantity ?? 1)
            const subtotal = item.subtotal != null ? Number(item.subtotal) : Number(item.price ?? 0) * qty
            return (
              <div key={idx} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 11, color: '#D1D5DB' }}>
                <span>{String(item.name ?? item.id ?? `商品 ${idx + 1}`)} × {qty}</span>
                <span style={{ color: '#9CA3AF' }}>{fmtUsd(subtotal, fallback.currency)}</span>
              </div>
            )
          })}
        </div>
      )}

      {sectionTitle(`关联审计事件${auditEvents.length ? `（${auditEvents.length}）` : ''}`)}
      {auditEvents.length === 0 ? (
        <div style={{ fontSize: 11, color: '#6B7280' }}>该订单暂未关联审计事件。</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {auditEvents.map((log, idx) => {
            const meta = EVENT_META[log.event] || { icon: '·', color: '#6B7280', label: log.event, core: false }
            return (
              <div key={idx} style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                <span style={{ color: meta.color, fontSize: 12, lineHeight: '16px' }}>{meta.icon}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 11, color: '#E5E7EB' }}>
                    {meta.label}
                    <span style={{ color: '#6B7280', marginLeft: 6 }}>{(log.timestamp || '').slice(11, 19)} UTC</span>
                  </div>
                  <EventDataSummary log={log} />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Meta({ label, value, color, mono }: { label: string; value: string; color?: string; mono?: boolean }) {
  return (
    <div>
      <div style={{ color: '#6B7280', fontSize: 10 }}>{label}</div>
      <div style={{
        color: color || '#E5E7EB', fontWeight: 600,
        fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, monospace' : undefined,
        wordBreak: 'break-all',
      }}>
        {value}
      </div>
    </div>
  )
}
