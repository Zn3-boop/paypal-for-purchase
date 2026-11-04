import type { AuditLogEntry } from '../../api'

/** Compact one-line summary per event type（审计时间轴与订单详情共用）。 */
export function EventDataSummary({ log }: { log: AuditLogEntry }) {
  const d = log.data || {}
  let text: string
  switch (log.event) {
    case 'constraint_changed':
      text = `${d.change_type || ''} ${d.constraint_type || ''} ${d.description || d.new_value || ''}`.trim()
      break
    case 'plan_generated':
      text = `方案 ${d.plan_id || '?'} · ${d.item_count ?? '?'} 件 · ${d.total ?? '?'} 元`
      break
    case 'cart_modified': {
      const modType = d.modification_type || ''
      const range = d.old_total != null && d.new_total != null ? ` ${d.old_total}→${d.new_total}` : ''
      text = `${modType}${range}`
      break
    }
    case 'review_confirmed':
      text = `确认 ${d.final_total} 元${d.order_id ? ` · 订单 ${String(d.order_id).slice(0, 10)}` : ''}${d.plan_id ? ` · 方案 ${d.plan_id}` : ''}`
      break
    case 'payment_completed':
      text = `支付 ${d.final_total} 元 · ${d.payment_method || 'paypal'}`
      break
    case 'order_status_changed': {
      const oid = String(d.order_id ?? '').slice(0, 10)
      const fromLabel = String(d.from_status || d.from || '')
      const toLabel = String(d.to_status || d.to || '')
      text = `订单 ${oid || '?'} ${fromLabel}→${toLabel}${d.reason ? ` · ${d.reason}` : ''}`
      break
    }
    case 'budget_violated':
      text = `超限 ${d.actual_total}/${d.budget_limit} · ${d.resolution || ''}`
      break
    default:
      text = Object.entries(d).slice(0, 2).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')
  }
  if (!text) return null
  return <div style={{ fontSize: 11, color: '#9CA3AF', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis' }}>{text}</div>
}