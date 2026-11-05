import { useEffect, useMemo, useState } from 'react'
import {
  fetchPlans,
  type Plan,
  type PlanItem,
  type PlansComparison,
  type PlansResponse,
} from '../../api'
import { fetchSplitPreview, type SplitPreviewResponse, createOrder, modifyCart } from '../../api'

type Props = {
  sessionId: string
  onSelectPlan: (planId: 'A' | 'B') => Promise<void>
  selectedPlanId: 'A' | 'B' | null
}

const fmtUsd = (n: number | undefined): string => `$${(n ?? 0).toFixed(2)}`

const round2 = (n: number): number => Math.round(n * 100) / 100

/** 商品唯一键：优先 id，缺 id 时退化为 name+price。 */
const itemKey = (item: PlanItem): string => item.id || `${item.name}:${item.price}`

/** 将后端 PlansResponse 归一化为组件 state。 */
function plansFrom(res: PlansResponse): { planA: Plan | null; planB: Plan | null } {
  return {
    planA: res.plans?.find(p => p.plan_id === 'A') || null,
    planB: res.plans?.find(p => p.plan_id === 'B') || null,
  }
}

export function PlansTab({ sessionId, onSelectPlan, selectedPlanId }: Props) {
  const [plans, setPlans] = useState<{ planA: Plan | null; planB: Plan | null }>({ planA: null, planB: null })
  const [comparison, setComparison] = useState<PlansComparison | null>(null)
  const [loading, setLoading] = useState(true)
  const [showingConfirm, setShowingConfirm] = useState<'A' | 'B' | null>(null)
  const [selecting, setSelecting] = useState<'A' | 'B' | null>(null)
  const [cardError, setCardError] = useState<string | null>(null)

  // 初始加载 + 5s 轮询（合并进同一个 effect，逻辑不重复，组件卸载后不再写 state）
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    fetchPlans(sessionId)
      .then(res => {
        if (!alive) return
        setPlans(plansFrom(res))
        setComparison(res.comparison || null)
      })
      .catch(err => console.error('Failed to fetch plans:', err))
      .finally(() => { if (alive) setLoading(false) })

    const interval = setInterval(() => {
      fetchPlans(sessionId)
        .then(res => {
          setPlans(plansFrom(res))
          setComparison(res.comparison || null)
        })
        .catch(() => { /* 轮询失败静默，等下一次轮询重试 */ })
    }, 5000)
    return () => { alive = false; clearInterval(interval) }
  }, [sessionId])

  if (loading && !plans.planA && !plans.planB) {
    return (
      <div style={{ padding: 24, textAlign: 'center', color: '#9CA3AF', fontSize: 13 }}>
        正在生成购物方案...
      </div>
    )
  }

  if (!plans.planA && !plans.planB) {
    return (
      <div style={{ background: '#0f172a', borderRadius: 12, padding: 20, border: '1px solid #374151', textAlign: 'center' }}>
        <div style={{ fontSize: 16, fontWeight: 600, color: '#FCD34D', marginBottom: 8 }}>⏳ 等待生成方案</div>
        <div style={{ fontSize: 12, color: '#9CA3AF' }}>请在上方约束看板输入需求后，方案将自动生成。</div>
      </div>
    )
  }

  return (
    <div style={{ background: '#0f172a', borderRadius: 12, padding: 16, border: '1px solid #374151' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: '#E5E7EB' }}>🗺️ 双方案对比</h3>
        <div style={{ fontSize: 11, color: '#9CA3AF' }}>选择任一方案后点击"确认购买"</div>
      </div>

      {cardError && (
        <div style={{ fontSize: 12, color: '#FCA5A5', marginBottom: 8 }}>⚠️ {cardError}</div>
      )}

      {/* Diff 视图：突出两方案差异项 */}
      {plans.planA && plans.planB && (
        <DiffView planA={plans.planA} planB={plans.planB} comparison={comparison} />
      )}

      {/* 双方案对比卡片（左右布局） */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {plans.planA && (
          <PlanCard
            plan={plans.planA}
            budgetLimit={getBudgetFromPlan(plans.planA)}
            onSelect={async () => {
              setSelecting('A')
              setCardError(null)
              try {
                await onSelectPlan('A')
                setShowingConfirm('A')
              } catch (err) {
                setCardError(err instanceof Error ? err.message : '选择方案 A 失败')
              } finally {
                setSelecting(null)
              }
            }}
            isSelected={selectedPlanId === 'A'}
            busy={selecting !== null}
          />
        )}
        {plans.planB && (
          <PlanCard
            plan={plans.planB}
            budgetLimit={getBudgetFromPlan(plans.planB)}
            onSelect={async () => {
              setSelecting('B')
              setCardError(null)
              try {
                await onSelectPlan('B')
                setShowingConfirm('B')
              } catch (err) {
                setCardError(err instanceof Error ? err.message : '选择方案 B 失败')
              } finally {
                setSelecting(null)
              }
            }}
            isSelected={selectedPlanId === 'B'}
            busy={selecting !== null}
          />
        )}
      </div>

      {/* 二次确认弹窗（带金额的"确认下单"按钮） */}
      {showingConfirm && (
        <SplitConfirmationModal
          key={showingConfirm}
          sessionId={sessionId}
          plan={showingConfirm === 'A' ? plans.planA! : plans.planB!}
          onClose={() => setShowingConfirm(null)}
        />
      )}
    </div>
  )
}

/** 按商品 ID 拆分 仅A / 共有 / 仅B，作为 Diff 视图的数据源。 */
function computeDiff(a: Plan, b: Plan): { onlyA: PlanItem[]; onlyB: PlanItem[]; common: PlanItem[] } {
  const itemsA = a.items || []
  const itemsB = b.items || []
  const keyA = new Set(itemsA.map(itemKey))
  const keyB = new Set(itemsB.map(itemKey))
  return {
    onlyA: itemsA.filter(i => !keyB.has(itemKey(i))),
    onlyB: itemsB.filter(i => !keyA.has(itemKey(i))),
    common: itemsA.filter(i => keyB.has(itemKey(i))),
  }
}

// Diff 视图：突出差异项（仅A / 共有 / 仅B + 价格差 + 推荐）
function DiffView({ planA, planB, comparison }: {
  planA: Plan
  planB: Plan
  comparison: PlansComparison | null
}) {
  const { onlyA, onlyB, common } = useMemo(() => computeDiff(planA, planB), [planA, planB])
  const delta = comparison?.total_delta ?? round2((planB.total || 0) - (planA.total || 0))
  const recommendation = comparison?.recommendation
  const [open, setOpen] = useState(true)

  return (
    <div style={{ border: '1px dashed #60A5FA66', borderRadius: 8, padding: 10, marginBottom: 12, background: '#0d1526' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontSize: 12, color: '#93C5FD', fontWeight: 700 }}
      >
        ⚖️ 差异对比
        <span style={{ fontSize: 11, color: '#9CA3AF', fontWeight: 500 }}>
          A 专属 {onlyA.length} · 共有 {common.length} · B 专属 {onlyB.length}
        </span>
        <span style={{ color: '#6B7280' }}>{open ? '▴' : '▾'}</span>
      </button>

      {open && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
            <DiffColumn title="仅方案 A" items={onlyA} color="#60A5FA" />
            <DiffColumn title="共有商品" items={common} color="#34D399" />
            <DiffColumn title="仅方案 B" items={onlyB} color="#FBBF24" />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11, color: '#9CA3AF' }}>
              价格差：
              <span style={{ color: delta > 0 ? '#FBBF24' : '#34D399', fontWeight: 700 }}>
                {delta > 0 ? `B 比 A 贵 ${fmtUsd(delta)}` : delta < 0 ? `A 比 B 贵 ${fmtUsd(-delta)}` : '两方案价格相同'}
              </span>
            </span>
            {recommendation && (
              <span style={{ fontSize: 11, color: '#C77DFF', background: '#1e1b4b', padding: '2px 8px', borderRadius: 99 }}>
                💡 {recommendation}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function DiffColumn({ title, items, color }: { title: string; items: PlanItem[]; color: string }) {
  return (
    <div style={{ background: '#111827', borderRadius: 6, padding: 6 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color, marginBottom: 4 }}>
        {title} ({items.length})
      </div>
      {items.length === 0 ? (
        <div style={{ fontSize: 10, color: '#4B5563', fontStyle: 'italic' }}>—</div>
      ) : (
        items.slice(0, 4).map((item, idx) => (
          <div key={item.id || idx} style={{ fontSize: 10, color: '#D1D5DB', marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            • {item.name}
          </div>
        ))
      )}
      {items.length > 4 && <div style={{ fontSize: 9, color: '#6B7280' }}>+{items.length - 4} 更多</div>}
    </div>
  )
}

// 单张方案卡片
function PlanCard({ plan, budgetLimit, onSelect, isSelected, busy }: {
  plan: Plan
  budgetLimit: number
  onSelect: () => void
  isSelected: boolean
  busy: boolean
}) {
  const budgetCheck = plan.budget_check
  const budgetOk = plan.budget_ok ?? true
  const total = plan.total ?? 0
  const budgetPercent = budgetLimit > 0 ? (total / budgetLimit) * 100 : 0
  const isOver = total > budgetLimit
  const overage = budgetCheck?.overage != null ? budgetCheck.overage : round2(total - budgetLimit)
  const guardrailAdjusted = (plan.risks || []).some(r => r.includes('guardrail') || r.includes('自动调整'))
  const suggestions = budgetCheck?.suggestions || []

  return (
    <div style={{
      background: isSelected ? '#1e3a5f' : '#1e293b',
      borderRadius: 10,
      padding: 14,
      border: isSelected ? '2px solid #3B82F6' : isOver ? '1px solid #EF4444' : '1px solid #374151',
      position: 'relative',
    }}>
      {/* 头部：方案名 + 价格高亮 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 700, color: '#E5E7EB' }}>方案 {plan.plan_id}</div>
          <div style={{ fontSize: 11, color: '#9CA3AF', marginTop: 2 }}>{plan.strategy}</div>
        </div>
        <div style={{ fontSize: 20, fontWeight: 700, color: budgetOk ? '#10B981' : isOver ? '#EF4444' : '#F59E0B' }}>
          {fmtUsd(total)}
        </div>
      </div>

      {/* 预算进度条（含超支提示） */}
      <div style={{ marginBottom: 10 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#9CA3AF', marginBottom: 4 }}>
          <span>预算 {fmtUsd(budgetLimit)}</span>
          <span>{budgetPercent.toFixed(0)}%{isOver ? ` · 超支 ${fmtUsd(overage)}` : ''}</span>
        </div>
        <div style={{ height: 6, background: '#374151', borderRadius: 3, overflow: 'hidden' }}>
          <div style={{
            width: `${Math.min(budgetPercent, 100)}%`,
            height: '100%',
            background: isOver ? '#EF4444' : budgetOk ? '#10B981' : '#F59E0B',
            transition: 'width 0.3s ease',
          }} />
        </div>
      </div>

      {/* Guardrail 状态黄色警示 */}
      {(guardrailAdjusted || !budgetOk) && (
        <div style={{ marginBottom: 8, fontSize: 10, color: '#FCD34D', background: '#1a1600', padding: 6, borderRadius: 4, border: '1px dashed #F59E0B66' }}>
          {guardrailAdjusted ? '⚡ Guardrail 已自动调整' : '🚨 Guardrail 预算拦截'}
          {budgetCheck?.resolution && <div style={{ marginTop: 2, opacity: 0.9 }}>{budgetCheck.resolution}</div>}
          {suggestions.slice(0, 2).map((s, i) => (
            <div key={i} style={{ marginTop: 2, opacity: 0.9 }}>
              💡 {s.original_item} → {s.replacement}（省 {fmtUsd(s.savings)}）
            </div>
          ))}
        </div>
      )}

      {/* 权衡说明 */}
      {(plan.trade_offs && plan.trade_offs.length > 0) && (
        <div style={{ marginBottom: 8, fontSize: 10, color: '#F59E0B', background: '#1a1a0a', padding: 6, borderRadius: 4 }}>
          ⚠️ {plan.trade_offs.slice(0, 2).map(t => t.split(':')[0]).join(' · ')}
        </div>
      )}

      {/* sacrificed_members 红色徽章 */}
      {plan.sacrificed_members && plan.sacrificed_members.length > 0 && (
        <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 9, fontWeight: 700, color: '#fff', background: '#DC2626', padding: '2px 8px', borderRadius: 99, letterSpacing: 0.5 }}>
            未满足 ×{plan.sacrificed_members.length}
          </span>
          {plan.sacrificed_members.map(m => (
            <span key={m} style={{ fontSize: 10, color: '#FCA5A5', background: '#2a0d0d', padding: '2px 8px', borderRadius: 99, border: '1px solid #EF444455' }}>
              {m}
            </span>
          ))}
        </div>
      )}

      {/* 商品清单折叠视图 */}
      <ItemList items={plan.items || []} />

      {/* 操作按钮 */}
      <button
        onClick={onSelect}
        disabled={isSelected || busy}
        style={{
          width: '100%',
          padding: '8px',
          borderRadius: 6,
          border: 'none',
          fontWeight: 600,
          cursor: (isSelected || busy) ? 'default' : 'pointer',
          background: isSelected ? '#064e3b' : busy ? '#1e3a5f' : '#3B82F6',
          color: '#fff',
          opacity: (isSelected || busy) ? 0.7 : 1,
          fontSize: 13,
        }}
      >
        {isSelected ? '已选择' : busy ? '选择中…' : '选择此方案'}
      </button>
    </div>
  )
}

// 商品清单折叠视图：默认显示前 3 件，可展开全部
function ItemList({ items }: { items: PlanItem[] }) {
  const [expanded, setExpanded] = useState(false)
  const list = items && items.length ? items : []
  const visible = expanded ? list : list.slice(0, 3)
  const hasMore = list.length > 3

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#9CA3AF', marginBottom: 4 }}>
        包含商品 ({list.length})
      </div>
      {list.length === 0 && <div style={{ fontSize: 10, color: '#6B7280', fontStyle: 'italic' }}>暂无商品</div>}
      {visible.map((item, idx) => (
        <div
          key={item.id || idx}
          style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 11, color: '#D1D5DB', marginBottom: 3, paddingLeft: 8, borderLeft: '2px solid #4B5563' }}
        >
          <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {item.name}
            {item.quantity > 1 && (
              <span style={{ color: '#FBBF24', marginLeft: 4, fontSize: 10 }}>×{item.quantity}</span>
            )}
          </span>
          <span style={{ color: '#E5E7EB', fontWeight: 600, flexShrink: 0 }}>
            {fmtUsd(item.subtotal ?? item.price * (item.quantity || 1))}
          </span>
        </div>
      ))}
      {hasMore && (
        <button
          onClick={() => setExpanded(e => !e)}
          style={{ display: 'block', fontSize: 10, color: '#60A5FA', background: 'none', border: 'none', cursor: 'pointer', marginTop: 2, padding: 0 }}
        >
          {expanded ? '▲ 收起' : `▼ 展开全部 ${list.length} 件`}
        </button>
      )}
    </div>
  )
}

// 二次确认弹窗（带金额的"确认下单"按钮）
function SplitConfirmationModal({ sessionId, plan, onClose }: {
  plan: Plan
  sessionId: string
  onClose: () => void
}) {
  const total = plan.total ?? 0
  const itemCount = plan.items?.length || 0
  const [preview, setPreview] = useState<SplitPreviewResponse | null>(null)
  const [previewLoading, setPreviewLoading] = useState(true)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [modalError, setModalError] = useState<string | null>(null)
  const [confirmDisabled, setConfirmDisabled] = useState(false)
  // 下单金额以**购物车**为准：预算拦截后可先改单，再在同一个弹窗里确认（服务端本来也只认购物车总价）
  const [cartTotal, setCartTotal] = useState<number>(total)
  const [cartCount, setCartCount] = useState<number>(itemCount)
  const [editInstruction, setEditInstruction] = useState('删掉木质相框、铝制奖牌、亚麻收纳袋')
  const [cartEditing, setCartEditing] = useState(false)
  const [editNote, setEditNote] = useState<string | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchSplitPreview(sessionId)
      .then(res => { if (alive) setPreview(res) })
      .catch(err => {
        if (!alive) return
        setPreviewError(err instanceof Error ? err.message : '分摊预览获取失败')
      })
      .finally(() => { if (alive) setPreviewLoading(false) })
    return () => { alive = false }
  }, [sessionId])

  const handleConfirm = async () => {
    if (confirmDisabled || previewLoading) return
    setConfirmDisabled(true)
    setModalError(null)
    try {
      const res = await createOrder({
        amount: cartTotal,
        session_id: sessionId,
        user_email: 'user@example.com',
        item_name: `Group Purchase Plan ${plan.plan_id}`,
      })
      window.location.href = res.approval_url
    } catch (err) {
      setModalError(err instanceof Error ? err.message : '下单失败，请稍后再试')
      setConfirmDisabled(false)
    }
  }

  /** 改单成功 = 服务端重算购物车；随后解锁"确认下单"，金额切到新总价。 */
  const handleModifyCart = async () => {
    const instruction = editInstruction.trim()
    if (!instruction || cartEditing) return
    setCartEditing(true)
    setEditError(null)
    setEditNote(null)
    try {
      const res = await modifyCart(sessionId, instruction)
      if (!res.success) {
        setEditError(res.message || res.guardrail?.message || '改单未生效，换个说法再试')
        return
      }
      const nextTotal = res.cart?.total
      const nextItems = res.cart?.items?.length
      if (typeof nextTotal === 'number') setCartTotal(nextTotal)
      if (typeof nextItems === 'number') setCartCount(nextItems)
      setModalError(null)          // 预算拦截红字清掉，确认键重新可用
      setConfirmDisabled(false)
      setEditNote(
        `已改单：${fmtUsd(res.diff?.old_total ?? total)} → ${fmtUsd(nextTotal ?? 0)}（${nextItems ?? 0} 件）`
        + ' · 审计新增 cart_modified',
      )
      // 分摊随金额变化，重新拉一次（失败不影响下单主链）
      setPreviewLoading(true)
      fetchSplitPreview(sessionId)
        .then(r => setPreview(r))
        .catch(() => { /* 忽略 */ })
        .finally(() => setPreviewLoading(false))
    } catch (err) {
      setEditError(err instanceof Error ? err.message : '改单失败')
    } finally {
      setCartEditing(false)
    }
  }

  return (
    <div
      style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.8)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
      onClick={onClose}
    >
      <div
        style={{ background: '#1f2937', borderRadius: 12, padding: 20, maxWidth: 400, width: '90%', boxShadow: '0 10px 40px rgba(0,0,0,0.5)' }}
        onClick={e => e.stopPropagation()}
      >
        <h3 style={{ margin: '0 0 12px', fontSize: 16, fontWeight: 700, color: '#E5E7EB' }}>确认下单</h3>

        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 11, color: '#9CA3AF', marginBottom: 4 }}>
            方案 {plan.plan_id}: {plan.strategy} · {cartCount} 件商品
          </div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#10B981' }}>总计 {fmtUsd(cartTotal)}</div>
          {Math.abs(cartTotal - total) > 0.005 && (
            <div style={{ fontSize: 10, color: '#9CA3AF', marginTop: 2 }}>
              方案快照 {fmtUsd(total)} → 购物车改单后 {fmtUsd(cartTotal)}
            </div>
          )}
        </div>

        {/* === F2 分摊预览：弹窗打开时 fetch，loading 禁用确认按钮 === */}
        <div style={{ marginTop: 12, marginBottom: 12 }}>
          <div style={{ fontSize: 11, color: '#9CA3AF', marginBottom: 6 }}>每人应付（按当前购物车金额均摊）：</div>
          {previewLoading ? (
            <div style={{ fontSize: 12, color: '#9CA3AF', fontStyle: 'italic' }}>正在获取分摊预览…</div>
          ) : previewError ? (
            <div style={{ fontSize: 12, color: '#FCA5A5', background: '#2a0d0d', padding: 6, borderRadius: 4 }}>
              ⚠️ {previewError}
            </div>
          ) : preview ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {preview.splits.map((split, i) => (
                <div key={i} style={{ fontSize: 12, color: '#E5E7EB', display: 'flex', justifyContent: 'space-between', padding: 4, background: '#374151', borderRadius: 4 }}>
                  <span>{split.name}</span>
                  <span style={{ fontWeight: 600, color: split.is_adjustment ? '#FCD34D' : '#10B981' }}>
                    {fmtUsd(split.amount)}{split.is_adjustment ? ' · 尾差' : ''}
                  </span>
                </div>
              ))}
              <div style={{ fontSize: 10, color: '#9CA3AF', marginTop: 4 }}>
                总计 {fmtUsd(preview.total)} · 平均 {fmtUsd(preview.per_person)} · 尾差由 {preview.adjustment_member || '最后一位成员'} 承担
              </div>
            </div>
          ) : (
            <div style={{ fontSize: 12, color: '#9CA3AF', fontStyle: 'italic' }}>暂无分摊数据</div>
          )}
        </div>

        {plan.budget_ok === false && (
          <div style={{ fontSize: 11, color: '#FCD34D', marginBottom: 8, background: '#1a1600', padding: 6, borderRadius: 4 }}>
            🚨 注意：该方案超出预算检查{plan.budget_check?.resolution ? ` — ${plan.budget_check.resolution}` : ''}
          </div>
        )}

        {plan.sacrificed_members && plan.sacrificed_members.length > 0 && (
          <div style={{ fontSize: 11, color: '#FCA5A5', marginBottom: 8, background: '#2a0d0d', padding: 6, borderRadius: 4 }}>
            ⚠️ 以下成员偏好未满足：<span style={{ color: '#EF4444', fontWeight: 600 }}>{plan.sacrificed_members.join(', ')}</span>
          </div>
        )}

        {plan.trade_offs && plan.trade_offs.length > 0 && (
          <div style={{ fontSize: 10, color: '#FCD34D', marginBottom: 8, background: '#1a1a0a', padding: 6, borderRadius: 4 }}>
            🔧 权衡：<br />
            {plan.trade_offs.slice(0, 2).map((t, i) => (
              <div key={i} style={{ marginLeft: 8, marginTop: 2 }}>• {t}</div>
            ))}
          </div>
        )}

        {modalError && (
          <div style={{ fontSize: 11, color: '#FCA5A5', marginBottom: 8, background: '#2a0d0d', padding: 6, borderRadius: 4 }}>
            ⚠️ 下单失败：<span style={{ color: '#EF4444', fontWeight: 600 }}>{modalError}</span>
          </div>
        )}

        {/* 购物车改单：预算拦截后可在同一弹窗里把 393 改到预算内，再点确认（Pinduoduo 式"改完直接付"） */}
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed #374151' }}>
          <div style={{ fontSize: 11, color: '#9CA3AF', marginBottom: 6 }}>
            🛒 购物车改单（可选）：例如「删掉木质相框、铝制奖牌、亚麻收纳袋」
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <input
              value={editInstruction}
              onChange={e => setEditInstruction(e.target.value)}
              disabled={cartEditing}
              style={{ flex: 1, padding: '7px 9px', borderRadius: 6, border: '1px solid #374151', background: '#111827', color: '#E5E7EB', fontSize: 12 }}
            />
            <button
              onClick={handleModifyCart}
              disabled={cartEditing || !editInstruction.trim()}
              style={{ padding: '7px 12px', borderRadius: 6, border: '1px solid #3B82F6', background: 'transparent', color: '#93C5FD', fontSize: 12, fontWeight: 600, cursor: cartEditing ? 'wait' : 'pointer', opacity: editInstruction.trim() ? 1 : 0.5 }}
            >
              {cartEditing ? '改单中…' : '执行改单'}
            </button>
          </div>
          {editNote && <div style={{ fontSize: 11, color: '#6EE7B7', marginTop: 6 }}>{editNote}</div>}
          {editError && <div style={{ fontSize: 11, color: '#FCA5A5', marginTop: 6 }}>⚠️ {editError}</div>}
        </div>

        <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
          <button
            onClick={onClose}
            style={{ flex: 1, padding: 10, borderRadius: 6, border: '1px solid #374151', background: 'transparent', color: '#9CA3AF', cursor: 'pointer', fontSize: 13 }}
          >
            取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={confirmDisabled || previewLoading || !!previewError || !!modalError}
            style={{ flex: 1, padding: 10, borderRadius: 6, border: 'none', background: '#10B981', color: '#fff', cursor: (confirmDisabled || previewLoading || !!previewError || !!modalError) ? 'not-allowed' : 'pointer', fontWeight: 600, fontSize: 13, opacity: (confirmDisabled || previewLoading || !!previewError || !!modalError) ? 0.5 : 1 }}
          >
            {previewLoading ? '计算分摊中…' : `确认下单 ${fmtUsd(cartTotal)}`}
          </button>
        </div>
      </div>
    </div>
  )
}

function getBudgetFromPlan(plan: Plan): number {
  return plan.budget_check?.limit ?? 600
}