import { useEffect, useState } from 'react'
import {
  fetchCart,
  fetchSplitPreview,
  removeCartItem,
  updateCartItemQuantity,
  type CartMutationResponse,
  type CartResponse,
  type SplitPreviewResponse,
} from '../api'

/**
 * 可见、可改的购物车面板 —— 结算链路的地基。
 *
 * 为什么必须有这个面板：之前「选方案 → 弹窗」里看不到购物车中间态，
 * 用户在方案之间切换（旧 select_plan 无条件重置）或改单后，不知道车里到底
 * 有什么、多少钱。现在：
 *   1. 数量 ± / 删除直接打 PATCH/DELETE（后端实时重算总额 + 审计留痕）；
 *   2. 预算超支：黄色提示 + 「去结算」按钮禁用（改到预算内自动恢复，把关点前移到面板）；
 *   3. 「去结算」打开复用的 SplitConfirmationModal（伪 Plan 喂进去）。
 */
type Props = {
  sessionId: string
  mode?: 'single' | 'group' | null
  /** 选方案 / 改单成功后 ++，强制重拉购物车（服务端唯一真相）。 */
  refreshKey?: number
  onCartChange?: (cart: CartResponse | null) => void
  onCheckout: () => void
}

const fmtUsd = (n: number | undefined): string => `$${(n ?? 0).toFixed(2)}`

export function CartPanel({ sessionId, mode, refreshKey = 0, onCartChange, onCheckout }: Props) {
  const [cart, setCart] = useState<CartResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [preview, setPreview] = useState<SplitPreviewResponse | null>(null)
  const isGroup = mode === 'group'

  // 拉购物车：refreshKey 变化（选方案/改单/删除后）即重拉。
  // 父组件传的 onCartChange 用 useCallback 稳定化，放依赖里不会抖。
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    setLoading(true)
    fetchCart(sessionId)
      .then(res => {
        if (!alive) return
        setCart(res)
        setError(null)
        setWarning(res.guardrail?.status === 'fail' ? (res.guardrail.message ?? null) : null)
        onCartChange?.(res)
      })
      .catch(err => {
        if (!alive) return
        setError(err instanceof Error ? err.message : '购物车加载失败')
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [sessionId, refreshKey, onCartChange])

  // 团购：每人应付预览（单选模式没有分摊这回事）
  useEffect(() => {
    if (!isGroup) {
      setPreview(null)
      return
    }
    let alive = true
    fetchSplitPreview(sessionId)
      .then(r => {
        if (alive) setPreview(r)
      })
      .catch(() => {
        if (alive) setPreview(null)
      })
    return () => {
      alive = false
    }
  }, [sessionId, isGroup, refreshKey])

  const applyResult = (res: CartMutationResponse) => {
    if (res.cart) {
      setCart(res.cart)
      onCartChange?.(res.cart)
    }
    setError(null)
    setWarning(res.guardrail?.status === 'fail' ? res.guardrail.message ?? null : null)
  }

  const changeQty = async (item: { id: string; name: string; quantity: number }, delta: number) => {
    const next = (item.quantity ?? 1) + delta
    if (next < 1 || busyId) return
    setBusyId(item.id)
    try {
      applyResult(await updateCartItemQuantity(sessionId, item.id, next))
    } catch (err) {
      setError(err instanceof Error ? err.message : '数量更新失败')
    } finally {
      setBusyId(null)
    }
  }

  const handleRemove = async (itemId: string) => {
    if (busyId) return
    setBusyId(itemId)
    try {
      applyResult(await removeCartItem(sessionId, itemId))
    } catch (err) {
      setError(err instanceof Error ? err.message : '移除失败')
    } finally {
      setBusyId(null)
    }
  }
const items = cart?.items ?? []
  const total = cart?.total ?? 0

  return (
    <div style={{ background: '#0f172a', borderRadius: 12, padding: 14, border: '1px solid #374151', marginBottom: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h3 style={{ margin: 0, fontSize: 13, fontWeight: 700, color: '#E5E7EB' }}>
          🛒 购物车<span style={{ color: '#6B7280', fontWeight: 500, marginLeft: 6 }}>{items.length} 件</span>
        </h3>
        <div style={{ fontSize: 14, fontWeight: 700, color: '#10B981' }}>{fmtUsd(total)}</div>
      </div>

      {loading ? (
        <div style={{ fontSize: 12, color: '#9CA3AF', fontStyle: 'italic' }}>正在读取购物车…</div>
      ) : error ? (
        <div style={{ fontSize: 12, color: '#FCA5A5', background: '#2a0d0d', padding: 8, borderRadius: 6, marginBottom: 8 }}>
          ⚠️ {error}
        </div>
      ) : items.length === 0 ? (
        <div style={{ fontSize: 12, color: '#6B7280', fontStyle: 'italic' }}>
          购物车还是空的 —— 在方案卡点「选择此方案」会把它装进车，之后可以在这里加减数量、删除，再结算。
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {items.map(item => (
              <div
                key={item.id}
                style={{ display: 'flex', alignItems: 'center', gap: 8, background: '#111827', borderRadius: 6, padding: '6px 8px' }}
              >
                <span style={{ flex: 1, minWidth: 0, fontSize: 12, color: '#E5E7EB', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {item.name}
                  <span style={{ color: '#6B7280', marginLeft: 6 }}>{fmtUsd(item.price)}/件</span>
                </span>
                <span style={{ fontSize: 11, fontWeight: 700, color: '#D1D5DB', minWidth: 56, textAlign: 'right' }}>
                  {fmtUsd(item.subtotal ?? (item.price ?? 0) * (item.quantity ?? 1))}
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <button
                    onClick={() => changeQty(item, -1)}
                    disabled={busyId !== null || (item.quantity ?? 1) <= 1}
                    style={{ width: 22, height: 22, borderRadius: 4, border: '1px solid #374151', background: '#1f2937', color: '#E5E7EB', cursor: 'pointer', fontSize: 12, lineHeight: 1 }}
                  >
                    −
                  </button>
                  <span style={{ fontSize: 12, color: '#E5E7EB', minWidth: 18, textAlign: 'center' }}>{item.quantity ?? 1}</span>
                  <button
                    onClick={() => changeQty(item, +1)}
                    disabled={busyId !== null}
                    style={{ width: 22, height: 22, borderRadius: 4, border: '1px solid #374151', background: '#1f2937', color: '#E5E7EB', cursor: 'pointer', fontSize: 12, lineHeight: 1 }}
                  >
                    +
                  </button>
                  <button
                    onClick={() => handleRemove(item.id)}
                    disabled={busyId !== null}
                    title="移除"
                    style={{ marginLeft: 4, width: 22, height: 22, borderRadius: 4, border: '1px solid #7f1d1d', background: '#3f1212', color: '#FCA5A5', cursor: 'pointer', fontSize: 11, lineHeight: 1 }}
                  >
                    🗑
                  </button>
                </span>
              </div>
            ))}
          </div>

          {warning && (
            <div style={{ fontSize: 11, color: '#FCD34D', background: '#4a380d', padding: '6px 8px', borderRadius: 6, marginTop: 8 }}>
              ⚠️ {warning}　<span style={{ color: '#9CA3AF' }}>可先在这里改量到预算内再结算（支付时按购物车重算复核）。</span>
            </div>
          )}

          {isGroup && preview && (
            <div style={{ fontSize: 11, color: '#9CA3AF', marginTop: 8 }}>
              每人应付约 <b style={{ color: '#93C5FD' }}>{fmtUsd(preview.per_person)}</b> · 尾差由{' '}
              <b style={{ color: '#FCD34D' }}>{preview.adjustment_member || '最后一位成员'}</b> 承担
            </div>
          )}

          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
            <button
              onClick={onCheckout}
              disabled={cart?.guardrail?.status === 'fail'}
              style={{
                fontSize: 13, padding: '6px 16px', borderRadius: 8, fontWeight: 700, cursor: cart?.guardrail?.status === 'fail' ? 'not-allowed' : 'pointer',
                border: 'none', background: cart?.guardrail?.status === 'fail' ? '#57534E' : '#10B981', color: cart?.guardrail?.status === 'fail' ? '#A8A29E' : '#052e1b',
              }}
            >
              {cart?.guardrail?.status === 'fail' ? '🚫 超预算 · 先改量再结算' : `💳 去结算（${fmtUsd(total)}）`}
            </button>
          </div>
        </>
      )}
    </div>
  )
}