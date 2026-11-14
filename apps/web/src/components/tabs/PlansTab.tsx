import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchPlans,
  regeneratePlans,
  type Plan,
  type PlanItem,
  type PlansFallbackInfo,
  type PlansResponse,
  type Satisfaction,
  type SatisfactionVerdict,
  type VerdictStatus,
} from '../../api'
import { fetchSplitPreview, fetchCart, type CartResponse, type SplitPreviewResponse, createOrder, modifyCart, addCartItem } from '../../api'
import { CartPanel } from '../CartPanel'

type Props = {
  sessionId: string
  onSelectPlan: (planId: 'A' | 'B', mode?: 'replace' | 'append') => Promise<void>
  selectedPlanId: 'A' | 'B' | null
  /** intent 提交后 ++，触发本 Tab 立即重拉方案（非轮询等待）。 */
  refreshKey?: number
  intentQuery?: string | null
  intentMode?: 'single' | 'group' | null
}

const fmtUsd = (n: number | undefined): string => `$${(n ?? 0).toFixed(2)}`

const round2 = (n: number): number => Math.round(n * 100) / 100

/**
 * 数据源人话化。后端字段保持 `channel3` / `k1_catalog` 原值（审计可复算），
 * 界面只负责翻译：直接把 `channel3` 甩给用户，等于让用户猜这是什么。
 */
const dataSourceLabel = (source?: string | null): string => {
  if (!source) return ''
  if (source === 'channel3') return '赞助商 Channel3'
  if (source === 'k1_catalog') return '本地示例目录'
  return source
}

/** 将后端 PlansResponse 归一化为组件 state。 */
function plansFrom(res: PlansResponse): { planA: Plan | null; planB: Plan | null } {
  return {
    planA: res.plans?.find(p => p.plan_id === 'A') || null,
    planB: res.plans?.find(p => p.plan_id === 'B') || null,
  }
}

export function PlansTab({
  sessionId,
  onSelectPlan,
  selectedPlanId,
  refreshKey = 0,
  intentQuery,
  intentMode,
}: Props) {
  const [plans, setPlans] = useState<{ planA: Plan | null; planB: Plan | null }>({ planA: null, planB: null })
  // 无解状态：feasible=false 时后端会带 fallback 说明 + 约束诊断，
  // 之前这里被丢掉了，用户只看到"⏳ 等待生成方案"——明明已经算完、结论是无解。
  const [infeasible, setInfeasible] = useState<{
    fallback?: PlansFallbackInfo
    satisfaction?: Satisfaction
    scope?: string
  } | null>(null)
  const [retrieval, setRetrieval] = useState<PlansResponse['retrieval']>(undefined)
  const [serverIntent, setServerIntent] = useState<{ query: string; mode: 'single' | 'group' } | null>(null)
  const [loading, setLoading] = useState(true)
  const [showingConfirm, setShowingConfirm] = useState<'A' | 'B' | 'cart' | null>(null)
  const [selecting, setSelecting] = useState<'A' | 'B' | null>(null)
  const [cardError, setCardError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)
  // 购物车中间态：面板可见、可改；选方案/改单后 ++ refreshKey 重拉
  const [cartSnapshot, setCartSnapshot] = useState<CartResponse | null>(null)
  const [cartRefreshKey, setCartRefreshKey] = useState(0)
  // 三个排序 tab 共用同一个推荐盒（不再有 A/B 两张对立卡）
  const [activeSort, setActiveSort] = useState<'cheap' | 'full' | 'best'>('cheap')

  // 检索依据：优先用后端回显（snapshot 重放时也有），不然用刚提交的提问原文
  const effectiveQuery = retrieval?.query || serverIntent?.query || intentQuery || ''
  const effectiveMode = serverIntent?.mode || intentMode || null

  /** CartPanel 回报的购物车快照 —— 结算弹窗里作为「伪 Plan」复用，无需新建弹窗组件。 */
  const handleCartChange = useCallback((cart: CartResponse | null) => {
    setCartSnapshot(cart)
  }, [])

  /**
   * 单品加购：只把「这一件」放进车里。
   *
   * 为什么需要：`select_plan` 是整份方案入车，对「我只想买一件」的用户意味着
   * 装进 8~16 件再逐个删。这里让方案卡里的每一行都能单独 ➕。
   * 后端只认方案快照内的商品（价格/来源以快照为准，购物车不藏假货）。
   */
  const handleAddItem = useCallback(async (item: PlanItem) => {
    try {
      await addCartItem(sessionId, item.id)
      setCartRefreshKey(k => k + 1)
      setCardError(null)
    } catch (err) {
      setCardError(err instanceof Error ? err.message : `加入「${item.name}」失败`)
    }
  }, [sessionId])

  const cartAsPlan: Plan = useMemo(
    () => ({
      plan_id: 'A',
      strategy: '当前购物车（方案快照 + 你的加购/改单）',
      items: cartSnapshot?.items ?? [],
      total: cartSnapshot?.total ?? 0,
      matched_constraints: [],
      unmet_constraints: [],
      trade_offs: [],
      risks: [],
      sacrificed_members: [],
    }),
    [cartSnapshot],
  )

  /** 唯一结算入口：有已选方案 → 弹方案确认；否则购物车非空 → 弹购物车确认。 */
  const openCheckout = () => {
    if (selectedPlanId) {
      setShowingConfirm(selectedPlanId)
      return
    }
    if ((cartSnapshot?.items?.length ?? 0) > 0) setShowingConfirm('cart')
  }

  /**
   * 三个排序 tab（最省/最全/最贴题）→ 同一个推荐盒：
   *   最省   = 方案 A（每品类最低价）
   *   最贴题 = 方案 B（同一候选池最高匹配分）
   *   最全   = 满足约束条数更多的那一个（A/B 里 satisfaction.passed 高者）
   * 没有第二个方案时该 tab 不出现 —— 绝不为了凑三个 tab 而编造一个"方案"。
   */
  const sortTabs = useMemo(() => {
    const tabs: { key: 'cheap' | 'full' | 'best'; label: string; plan: Plan }[] = []
    if (plans.planA) tabs.push({ key: 'cheap', label: '💰 最省', plan: plans.planA })
    if (plans.planB) tabs.push({ key: 'best', label: '🎯 最贴题', plan: plans.planB })
    if (plans.planA && plans.planB) {
      const a = plans.planA.satisfaction?.passed ?? -1
      const b = plans.planB.satisfaction?.passed ?? -1
      if (a >= 0 || b >= 0) {
        const fullest = b > a ? plans.planB : plans.planA
        // 最全和最省/最贴题是同一份候选集时，不重复出 tab（避免"两个 tab 内容一样"）
        if (!tabs.some(t => t.plan.plan_id === fullest.plan_id)) {
          tabs.push({ key: 'full', label: '🏆 最全', plan: fullest })
        }
      }
    }
    return tabs
  }, [plans])

  const activePlan = sortTabs.find(t => t.key === activeSort)?.plan ?? sortTabs[0]?.plan ?? null

  // 初始加载 + 自适应轮询（等新方案时 1.5s，稳定后 5s）+ intent 提交后立即刷新
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    let tries = 0

    // 新检索词 = 新任务：先把上一问的卡片清掉并进入"生成中"。
    // 否则旧方案会一直挂在屏幕上，用户看到的就是"换了问题还是旧推荐"。
    setPlans({ planA: null, planB: null })
    setRetrieval(undefined)
    setInfeasible(null)
    setLoading(true)
    tries = 0

    /**
     * 落地前先校验快照是不是**本次提问**的：
     * 提交新需求时，上一轮（带 Channel3 检索，可能几秒到几十秒）仍可能在快照失效后落盘，
     * GET 就会把上一问的方案回放出来——表现就是"换了问题却还是旧方案，要手动刷新"。
     * 所以查询词对不上就不落地，按 1.5s 快跑重试，新快照一到立刻替换。
     *
     * 注意比较必须 trim 后再比：后端 `submit_intent` 会把 message `.strip()` 后存成
     * intent_query，而这里接到的 intentQuery 是用户在输入框里打的**原文**。只要首尾
     * 有空格（中文输入法的全角空格、复制粘贴带换行），两边就永远不相等，表现是
     * 审计里方案早生成了，页面却一直停在「⏳ 等待生成方案」。
     */
    const apply = (res: PlansResponse): boolean => {
      const norm = (s?: string | null) => (s ?? '').trim()
      const respQuery = norm(res.retrieval?.query)
      if (norm(intentQuery) && respQuery && respQuery !== norm(intentQuery)) return false
      const normalized = plansFrom(res)
      setPlans(normalized)
      setRetrieval(res.retrieval)
      // 「算完了但没有可行组合」是一个结论，不是一个中间态：必须落地成可展示的卡片，
      // 否则前端会一直停在"等待生成方案"，用户永远等不到东西。
      setInfeasible(
        res.feasible === false && !normalized.planA && !normalized.planB
          ? { fallback: res.fallback, satisfaction: res.satisfaction, scope: res.satisfaction_scope }
          : null,
      )
      if (res.intent_query && (res.intent_mode === 'single' || res.intent_mode === 'group')) {
        setServerIntent({ query: res.intent_query, mode: res.intent_mode })
      }
      return true
    }

    const pullOnce = async (): Promise<boolean> => {
      try {
        const res = await fetchPlans(sessionId)
        if (!alive) return true
        tries += 1
        const ok = apply(res)
        if (ok) {
          setLoading(false)
        } else if (tries >= 20) {
          // 等太久就先放掉 loading（文案回到"等待生成"），但轮询继续
          setLoading(false)
        }
        return ok
      } catch {
        // 网络抖动不算"查询词不符"，按正常节奏重试
        tries += 1
        if (tries >= 20) setLoading(false)
        return true
      }
    }

    const loop = (delay: number) => {
      if (!alive) return
      timer = setTimeout(() => {
        void pullOnce().then(ok => loop(ok ? 5000 : 1500))
      }, delay)
    }
    loop(0)

    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [sessionId, refreshKey, intentQuery])

  /**
   * 主动重算：约束变动不会静默改写快照（治理要求），所以给用户一个显式出口。
   * 顺序必须是「先让后端失效快照 → 再 GET」，否则 GET 只会回放旧快照（刷新无效的根因）。
   */
  const onRegenerate = () => {
    if (!sessionId || regenerating) return
    setRegenerating(true)
    setCardError(null)
    regeneratePlans(sessionId)
      .then(() => fetchPlans(sessionId))
      .then(res => {
        const normalized = plansFrom(res)
        setPlans(normalized)
        setRetrieval(res.retrieval)
        setInfeasible(
          res.feasible === false && !normalized.planA && !normalized.planB
            ? { fallback: res.fallback, satisfaction: res.satisfaction, scope: res.satisfaction_scope }
            : null,
        )
        if (res.intent_query && (res.intent_mode === 'single' || res.intent_mode === 'group')) {
          setServerIntent({ query: res.intent_query, mode: res.intent_mode })
        }
      })
      .catch(err => {
        const msg = err instanceof Error ? err.message : '重新生成失败'
        // 405 几乎只发生在「后端还跑着加路由之前的老进程」：/plans/regenerate 不存在，
        // POST 被 SPA 的 GET 兜底路由接住 → Method Not Allowed。直接说人话，别让用户干瞪眼。
        setCardError(/Method Not Allowed/i.test(msg)
          ? '后端还是旧代码（缺少 /plans/regenerate）。重启 uvicorn 后再点一次即可。'
          : msg)
      })
      .finally(() => setRegenerating(false))
  }

  if (loading && !plans.planA && !plans.planB) {
    return (
      <div style={{ padding: 24, textAlign: 'center', color: '#9CA3AF', fontSize: 13 }}>
        {intentQuery
          ? <>正在按「{intentQuery}」检索并生成方案…<div style={{ fontSize: 11, marginTop: 6, color: '#6B7280' }}>实时向 Channel3 检索，通常几秒；换问题会直接替换这里的内容</div></>
          : '正在生成购物方案...'}
      </div>
    )
  }

  if (!plans.planA && !plans.planB) {
    // 无解是有结论的失败：讲清楚"卡在哪条约束、候选池里有什么、能怎么改"，
    // 而不是永远显示"等待生成方案"（那会让用户以为还在算，实际早就返回了）。
    if (infeasible) {
      return (
        <InfeasibleCard
          fallback={infeasible.fallback}
          satisfaction={infeasible.satisfaction}
          scope={infeasible.scope}
          retrieval={retrieval}
          onRegenerate={onRegenerate}
          regenerating={regenerating}
        />
      )
    }
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
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: '#E5E7EB' }}>🛒 推荐组合</h3>
        {selectedPlanId ? (
          <button
            onClick={openCheckout}
            style={{ fontSize: 11, padding: '4px 10px', borderRadius: 99, border: '1px solid #10B981', background: 'transparent', color: '#6EE7B7', cursor: 'pointer', fontWeight: 700 }}
          >
            💳 确认并支付方案 {selectedPlanId}
          </button>
        ) : (cartSnapshot?.items?.length ?? 0) > 0 ? (
          <button
            onClick={openCheckout}
            style={{ fontSize: 11, padding: '4px 10px', borderRadius: 99, border: '1px solid #10B981', background: 'transparent', color: '#6EE7B7', cursor: 'pointer', fontWeight: 700 }}
          >
            💳 结算购物车（{cartSnapshot?.items?.length ?? 0} 件 · {fmtUsd(cartSnapshot?.total)}）
          </button>
        ) : (
          <div style={{ fontSize: 11, color: '#9CA3AF' }}>点「选择此方案」后在这里确认支付</div>
        )}
      </div>

      {/* 推荐依据：这是「你按哪个关键词推荐」的正面回答——
          输入框的原文就是 Channel3 的检索词，命中数原样回显，不加工不猜测。 */}
      {effectiveQuery && (
        <div style={{
          marginBottom: 10, padding: '6px 10px', borderRadius: 6,
          background: '#0b1220', border: '1px dashed #3B82F655',
          display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, fontSize: 11,
        }}>
          <span style={{ color: '#93C5FD', fontWeight: 700 }}>🔎 推荐依据</span>
          <span style={{ color: '#E5E7EB' }}>关键词「{effectiveQuery}」</span>
          {retrieval?.search_query && retrieval.search_query !== effectiveQuery && (
            <span style={{ color: '#6B7280' }}>
              实际检索词「{retrieval.search_query}」
            </span>
          )}
          {effectiveMode && (
            <span style={{ color: effectiveMode === 'group' ? '#FBBF24' : '#34D399' }}>
              {effectiveMode === 'group' ? '👥 团购模式' : '🧍 单人模式'}
            </span>
          )}
          {retrieval?.source && <span style={{ color: '#9CA3AF' }}>数据源 {dataSourceLabel(retrieval.source)}</span>}
          {/* 三态徽标：error 是"暂时不可用"（可重试），empty 是"商品库真没匹配"，
              两者文案与颜色都不同，避免把可恢复故障说成产品缺陷。 */}
          {retrieval?.channel3_status === 'error' && (
            <span style={{ color: '#F87171' }} title={retrieval?.note || ''}>
              · 赞助商检索暂时不可用
            </span>
          )}
          {retrieval?.channel3_status === 'empty' && (
            <span style={{ color: '#FCD34D' }}>· 赞助商商品库无匹配</span>
          )}
          {retrieval?.channel3_status === 'disabled' && (
            <span style={{ color: '#9CA3AF' }}>· 赞助商检索未启用</span>
          )}
          {typeof retrieval?.candidate_count === 'number' && (
            <span style={{ color: '#9CA3AF' }}>
              召回 {retrieval.candidate_count} 件 · 命中 {retrieval.matched_count ?? 0} 件
              {typeof retrieval?.filtered_count === 'number' && retrieval.filtered_count > 0 && (
                <> · 过滤 {retrieval.filtered_count} 件</>
              )}
            </span>
          )}
          {retrieval?.note && (
            <span style={{ color: '#FCD34D' }}>· {retrieval.note}</span>
          )}
          {(effectiveQuery && typeof retrieval?.candidate_count === 'number' && retrieval.candidate_count === 0) && (
            <span style={{ color: '#FCD34D' }}>Channel3 未命中，已回落本地目录</span>
          )}
          {effectiveQuery && (
            <button
              onClick={onRegenerate}
              disabled={regenerating}
              style={{ marginLeft: 'auto', fontSize: 11, background: '#1e3a8a', color: '#BFDBFE', border: '1px solid #2563EB66', borderRadius: 99, padding: '3px 12px', cursor: regenerating ? 'default' : 'pointer' }}
              title="改完约束 / 换口味后，主动重算方案（快照不会因约束变动而静默改写）"
            >
              {regenerating ? '⏳ 重算中…' : '🔄 重新生成方案'}
            </button>
          )}
        </div>
      )}

      {cardError && (
        <div style={{ fontSize: 12, color: '#FCA5A5', marginBottom: 8 }}>⚠️ {cardError}</div>
      )}

      {/* 🤝 赞助商相关推荐：Channel3 是赞助商，但「相关」不等于「匹配」——
          只有它真被检索命中的商品才进方案；这里展示的是"最接近但未计入方案"的那几件，
          避免把黑胶唱片当体育用品塞进方案。 */}
      {(retrieval?.sponsor_related?.length ?? 0) > 0 && (
        <div style={{
          marginBottom: 12, padding: '8px 10px', borderRadius: 8,
          background: '#0b1220', border: '1px solid #8B5CF666',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: '#C4B5FD' }}>🤝 赞助商 Channel3 相关推荐</span>
            <span style={{ fontSize: 10, color: '#9CA3AF' }}>
              {retrieval?.sponsor_note || '不完全匹配本次需求，未计入方案，仅供单独参考'}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8, overflowX: 'auto', paddingBottom: 2 }}>
            {retrieval!.sponsor_related!.map(sp => (
              <div key={sp.id} style={{
                flex: '0 0 150px', background: '#111827', border: '1px solid #374151',
                borderRadius: 6, padding: 8, display: 'flex', flexDirection: 'column', gap: 4,
              }}>
                <div style={{ fontSize: 11, color: '#E5E7EB', fontWeight: 600, lineHeight: 1.35, maxHeight: 44, overflow: 'hidden' }}>
                  {sp.name}
                </div>
                {(sp as any).offer_url && (
                  <a
                    href={(sp as any).offer_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`在商家页面查看${(sp as any).domain ? `（${(sp as any).domain}）` : ''}`}
                    style={{ color: '#93C5FD', fontSize: 9, textDecoration: 'none' }}
                  >
                    ↗ 商家页查看
                  </a>
                )}
                <div style={{ fontSize: 12, color: '#6EE7B7', fontWeight: 700 }}>{fmtUsd(sp.price)}</div>
                {sp.category && <div style={{ fontSize: 9, color: '#6B7280' }}>{sp.category}</div>}
                {(sp.tags || []).slice(0, 2).length > 0 && (
                  <div style={{ fontSize: 9, color: '#9CA3AF', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {sp.tags!.slice(0, 2).join(' · ')}
                  </div>
                )}
                <div style={{ fontSize: 9, color: '#8B5CF6', marginTop: 'auto' }}>Channel3 赞助商目录</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 三个排序 tab 共用**同一个**推荐盒：切换只改排序，不产生"方案 A vs 方案 B"的对立卡。
          没有第二个方案时只渲染一个 tab —— 不为了凑数编造方案。 */}
      {sortTabs.length > 1 && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
          {sortTabs.map(tab => {
            const isActive = (activePlan?.plan_id ?? '') === tab.plan.plan_id
            return (
              <button
                key={tab.key}
                onClick={() => setActiveSort(tab.key)}
                style={{
                  fontSize: 12, padding: '5px 12px', borderRadius: 99, cursor: 'pointer', fontWeight: 600,
                  background: isActive ? '#1d4ed8' : 'transparent',
                  border: isActive ? '1px solid #3B82F6' : '1px solid #374151',
                  color: isActive ? '#DBEAFE' : '#9CA3AF',
                }}
                title={
                  tab.key === 'cheap' ? '每个品类取最低价（总价最小）'
                    : tab.key === 'best' ? '同一候选池里关键词匹配分最高'
                      : '满足约束条数最多'
                }
              >
                {tab.label}
                {tab.key === 'full' && (tab.plan.satisfaction?.total ?? 0) > 0 && (
                  <span style={{ marginLeft: 4, opacity: 0.8 }}>
                    {tab.plan.satisfaction?.passed}/{tab.plan.satisfaction?.total}
                  </span>
                )}
              </button>
            )
          })}
        </div>
      )}

      {/* 一个大的推荐盒：组合级「满足 N/M 条」+ 逐条 ✅/❌/❔ + 展开逐件 ✅ */}
      {activePlan && (
        <PlanCard
          plan={activePlan}
          budgetLimit={getBudgetFromPlan(activePlan)}
          onSelect={async () => {
            setSelecting(activePlan.plan_id)
            setCardError(null)
            try {
              // 购物车非空时用 append（合并数量）而不是 replace：不静默清掉用户手加的件
              await onSelectPlan(activePlan.plan_id, (cartSnapshot?.items?.length ?? 0) > 0 ? 'append' : 'replace')
              setCartRefreshKey(k => k + 1)
              setShowingConfirm(activePlan.plan_id)
            } catch (err) {
              setCardError(err instanceof Error ? err.message : `选择方案 ${activePlan.plan_id} 失败`)
            } finally {
              setSelecting(null)
            }
          }}
          isSelected={selectedPlanId === activePlan.plan_id}
          busy={selecting !== null}
          onReopen={() => setShowingConfirm(activePlan.plan_id)}
          onAddItem={handleAddItem}
        />
      )}

      {/* 可见可改的购物车中间态：加减数量 / 删除 / 预算事实 / 去结算 */}
      <CartPanel
        sessionId={sessionId}
        mode={effectiveMode}
        refreshKey={cartRefreshKey}
        onCartChange={handleCartChange}
        onCheckout={openCheckout}
      />

      {/* 二次确认弹窗（带金额的"确认下单"按钮）；购物车模式喂「伪 Plan」复用同一弹窗 */}
      {showingConfirm && (showingConfirm !== 'cart' || (cartSnapshot?.items?.length ?? 0) > 0) && (
        <SplitConfirmationModal
          key={showingConfirm}
          sessionId={sessionId}
          mode={effectiveMode}
          plan={showingConfirm === 'cart' ? cartAsPlan : showingConfirm === 'A' ? plans.planA! : plans.planB!}
          onClose={() => setShowingConfirm(null)}
        />
      )}
    </div>
  )
}
function PlanCard({ plan, budgetLimit, onSelect, onReopen, onAddItem, isSelected, busy }: {
  plan: Plan
  budgetLimit: number | null
  onSelect: () => void
  onReopen?: () => void
  onAddItem?: (item: PlanItem) => Promise<void>
  isSelected: boolean
  busy: boolean
}) {
  const budgetCheck = plan.budget_check
  const budgetOk = plan.budget_ok ?? true
  const total = plan.total ?? 0
  // 会话没有设置硬预算时 budgetLimit 为 null：不再显示假的「预算 $600」进度条
  const limit = budgetLimit != null && budgetLimit > 0 ? budgetLimit : null
  const budgetPercent = limit != null ? (total / limit) * 100 : 0
  const isOver = limit != null && total > limit
  const overage = budgetCheck?.overage != null ? budgetCheck.overage : (limit != null ? round2(total - limit) : 0)
  const guardrailAdjusted = (plan.risks || []).some(r => r.includes('guardrail') || r.includes('自动调整'))
  const suggestions = budgetCheck?.suggestions || []

  return (
    <div style={{
      background: isSelected ? '#1e3a5f' : '#1e293b',
      borderRadius: 10,
      padding: 14,
      minWidth: 0,
      overflow: 'hidden',
      border: isSelected ? '2px solid #3B82F6' : isOver ? '1px solid #EF4444' : '1px solid #374151',
      position: 'relative',
    }}>
      {/* 头部：策略名 + 满足条数 + 价格高亮（不再用「方案 A/B」这种对立命名） */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: '#E5E7EB', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {plan.strategy || `方案 ${plan.plan_id}`}
          </div>
          <div style={{ fontSize: 11, color: '#9CA3AF', marginTop: 2 }}>
            {plan.satisfaction?.judgeable
              ? `满足约束 ${plan.satisfaction.passed}/${plan.satisfaction.total} 条`
              : '未声明可判定约束'}
            {(plan.satisfaction?.hidden_failures?.length ?? 0) > 0
              ? ` · 另有 ${plan.satisfaction!.hidden_failures.length} 条不满足已折叠`
              : ''}
          </div>
        </div>
        <div style={{ fontSize: 20, fontWeight: 700, color: budgetOk ? '#10B981' : isOver ? '#EF4444' : '#F59E0B', flexShrink: 0 }}>
          {fmtUsd(total)}
        </div>
      </div>

      {/* P0-3 软预算上限：无明确预算时「最贴题」超最省方案 3 倍 → 折叠提示，不冒充推荐 */}
      {plan.soft_cap_exceeded && (
        <div style={{ marginBottom: 8, fontSize: 10, color: '#FCA5A5', background: '#2a0d0d', padding: 6, borderRadius: 4, border: '1px dashed #EF444466' }}>
          ⚠️ {plan.soft_cap_note || '「最贴题」总价超出软预算上限，已折叠'}
        </div>
      )}

      {/* 数量诚实（评审盯点②）：需求 N 件只找到 M 件时列明，方案不冒充「需求已满足」 */}
      {plan.quantity_shortfall && (
        <div style={{ marginBottom: 8, fontSize: 10, color: '#FCD34D', background: '#1f1a08', padding: 6, borderRadius: 4, border: '1px dashed #F59E0B66' }}>
          {plan.quantity_note || '数量未满足：候选不足，未静默凑数'}
        </div>
      )}

      {/* 约束判定（✅/❌/❔ + 可复算证据）：这是「为什么推荐」的正面回答 */}
      <SatisfactionBlock satisfaction={plan.satisfaction} />

      {/* 预算进度条（含超支提示）；未设置预算时只显示总价，不编造预算上限 */}
      {limit == null ? (
        <div style={{ marginBottom: 10, fontSize: 10, color: '#6B7280' }}>
          未设置预算上限（可在上方约束看板补充，例如「总预算 600 美元」）
        </div>
      ) : (
        <div style={{ marginBottom: 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#9CA3AF', marginBottom: 4 }}>
            <span>预算 {fmtUsd(limit)}</span>
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
      )}

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
          ⚠️ {plan.trade_offs.slice(0, 2).join(' · ')}
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

      {/* 商品清单折叠视图（逐件 ✅/❌ 徽标） */}
      <ItemList items={plan.items || []} satisfaction={plan.satisfaction} onAddItem={onAddItem} />

      {/* 操作按钮：未选 → 选方案；已选 → 直接重开结算弹窗（不再变成死按钮） */}
      <button
        onClick={isSelected ? (onReopen ?? onSelect) : onSelect}
        disabled={busy}
        style={{
          width: '100%',
          padding: '8px',
          borderRadius: 6,
          border: 'none',
          fontWeight: 600,
          cursor: busy ? 'default' : 'pointer',
          background: isSelected ? '#064e3b' : busy ? '#1e3a5f' : '#3B82F6',
          color: '#fff',
          opacity: busy ? 0.7 : 1,
          fontSize: 13,
        }}
      >
        {isSelected ? '✓ 已选择 · 点此继续支付' : busy ? '选择中…' : '选择此方案'}
      </button>
    </div>
  )
}

// 商品清单折叠视图：默认显示前 3 件，可展开全部。
// **单品层不做「总判定」** —— 行首红叉曾被误读成「这件不行」，实际只是「这条判不了」。
// 现在改成逐条标签（✅ 命中 / ❔ 不可判），全组共性在顶部写一次（8/8 都是呢绒 → 只写一行）。
function ItemList({ items, satisfaction, onAddItem }: { items: PlanItem[]; satisfaction?: Satisfaction; onAddItem?: (item: PlanItem) => Promise<void> }) {
  const [expanded, setExpanded] = useState(false)
  const [addingId, setAddingId] = useState<string | null>(null)
  const list = items && items.length ? items : []
  const visible = expanded ? list : list.slice(0, 3)
  const hasMore = list.length > 3
  const rowsById = useMemo(() => {
    const map = new Map<string, Satisfaction['items'][number]>()
    ;(satisfaction?.items || []).forEach(r => { if (r.item_id) map.set(r.item_id, r) })
    return map
  }, [satisfaction])
  // P1-④ 共性上提：全组每件都 ✅ 的约束只在顶部写一次，单品行不重复刷屏
  const commonIds = useMemo(() => {
    const rows = (satisfaction?.items || []).filter(r => list.some(it => it.id === r.item_id))
    const common = new Set<string>()
    if (rows.length < 2) return common
    for (const r of rows[0].results || []) {
      if (r.status !== 'pass') continue
      const allPass = rows.every(row => row.results.find(x => x.constraint_id === r.constraint_id)?.status === 'pass')
      if (allPass) common.add(r.constraint_id)
    }
    return common
  }, [satisfaction, list])
  const commonLabels = useMemo(() => {
    const contentById = new Map((satisfaction?.constraints || []).map(c => [c.constraint_id, c.content]))
    return Array.from(commonIds).map(id => contentById.get(id)).filter((t): t is string => !!t)
  }, [commonIds, satisfaction])
  // 逐件标签：pass → ✅ / taboo 违规 → ❌ / 其余 fail 与 n/a 不当红叉（语义是「判不了」）
  const chipsFor = (item: PlanItem) => {
    const row = item.id ? rowsById.get(item.id) : undefined
    if (!row) return null
    const results = row.results || []
    const chips = results
      .filter(r => !commonIds.has(r.constraint_id))
      .filter(r => r.status === 'pass' || r.status === 'n/a' || (r.type === 'taboo' && r.status === 'fail'))
    const hasEvidence = results.some(r => r.status === 'pass' || (r.type === 'taboo' && r.status === 'fail'))
    return { chips, hasEvidence }
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#9CA3AF', marginBottom: 4 }}>
        包含商品 ({list.length})
      </div>
      {list.length === 0 && <div style={{ fontSize: 10, color: '#6B7280', fontStyle: 'italic' }}>暂无商品</div>}
      {commonLabels.length > 0 && (
        <div style={{ fontSize: 9, color: '#6EE7B7', marginBottom: 4, paddingLeft: 8 }}>
          全部 {list.length} 件：{commonLabels.map(t => `✅ ${t}`).join(' · ')}
        </div>
      )}
      {visible.map((item, idx) => {
        const chips = chipsFor(item)
        return (
        <div
          key={item.id || idx}
          style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#D1D5DB', marginBottom: 4, paddingLeft: 8, borderLeft: '2px solid #4B5563' }}
        >
          {item.image_url ? (
            <img
              src={item.image_url}
              alt={item.name}
              loading="lazy"
              style={{ width: 26, height: 26, borderRadius: 4, objectFit: 'cover', flexShrink: 0, background: '#1f2937' }}
              onError={e => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
            />
          ) : (
            <span style={{ width: 26, height: 26, borderRadius: 4, background: '#1f2937', flexShrink: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, color: '#4B5563' }}>🛍️</span>
          )}
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {item.name}
              {(item as any).offer_url ? (
                <a
                  href={(item as any).offer_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  title={`在商家页面查看${(item as any).domain ? `（${(item as any).domain}）` : ''}`}
                  style={{ color: '#93C5FD', marginLeft: 6, textDecoration: 'none', fontSize: 10 }}
                >
                  ↗ 商家页
                </a>
              ) : null}
              {item.quantity > 1 && (
                <span style={{ color: '#FBBF24', marginLeft: 4, fontSize: 10 }}>×{item.quantity}</span>
              )}
            </span>
            {/* P0-1 逐条标签：只说「这条命中了什么」，不打「这件不行」的总判定 */}
            {chips && (
              <span style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 2 }}>
                {chips.hasEvidence ? (
                  chips.chips.map(c => {
                    const icon = c.status === 'pass' ? '✅' : c.type === 'taboo' && c.status === 'fail' ? '❌' : '❔'
                    const color = icon === '✅' ? '#6EE7B7' : icon === '❌' ? '#FCA5A5' : '#9CA3AF'
                    return (
                      <span
                        key={c.constraint_id}
                        title={c.evidence}
                        style={{ fontSize: 9, color, border: `1px solid ${color}44`, borderRadius: 99, padding: '0 6px', whiteSpace: 'nowrap' }}
                      >
                        {icon} {c.label}
                      </span>
                    )
                  })
                ) : (
                  <span style={{ fontSize: 9, color: '#6B7280', fontStyle: 'italic' }}>未命中任何可判定约束</span>
                )}
              </span>
            )}
            {(item.match_score != null || (item.tags && item.tags.length > 0)) && (
              <span style={{ display: 'block', fontSize: 9, color: '#6B7280', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {item.match_score != null ? `关键词匹配 ${Math.round((item.match_score || 0) * 100) / 100}` : ''}
                {item.tags && item.tags.length > 0 ? ` · ${item.tags.slice(0, 3).join(' / ')}` : ''}
              </span>
            )}
          </span>
          <span style={{ color: '#E5E7EB', fontWeight: 600, flexShrink: 0 }}>
            {fmtUsd(item.subtotal ?? item.price * (item.quantity || 1))}
          </span>
          {onAddItem && (
            <button
              onClick={async e => {
                e.stopPropagation()
                if (addingId || !item.id) return
                setAddingId(item.id)
                try { await onAddItem(item) } finally { setAddingId(null) }
              }}
              disabled={!item.id || addingId !== null}
              title={item.id ? `只加入「${item.name}」这一件到购物车` : '该商品缺少 id，无法单独加购'}
              style={{
                flexShrink: 0, width: 20, height: 20, borderRadius: 6, cursor: item.id && !addingId ? 'pointer' : 'not-allowed',
                border: '1px solid #3B82F6', background: '#1E3A5F', color: '#93C5FD',
                fontSize: 12, fontWeight: 700, lineHeight: 1, padding: 0,
                opacity: item.id && !addingId ? 1 : 0.45,
              }}
            >
              {addingId === item.id ? '…' : '＋'}
            </button>
          )}
        </div>
        )
      })}
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

// 无可行方案卡片：把"为什么无解"讲清楚 —— 回退说明 + 逐条约束诊断 + 可操作的调整建议。
// 红线：无解就是无解，绝不为了"页面上有东西"塞一堆无关商品冒充方案。
function InfeasibleCard({
  fallback,
  satisfaction,
  scope,
  retrieval,
  onRegenerate,
  regenerating,
}: {
  fallback?: PlansFallbackInfo
  satisfaction?: Satisfaction
  scope?: string
  retrieval?: PlansResponse['retrieval']
  onRegenerate?: () => void
  regenerating?: boolean
}) {
  const suggestions = fallback?.suggestions || []
  const channel3Down = retrieval?.channel3_status === 'error'
  return (
    <div style={{ background: '#0f172a', borderRadius: 12, padding: 16, border: '1px solid #7F1D1D' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: '#FCD34D' }}>
          ⚠️ 当前约束下没有可行组合
        </h3>
        {onRegenerate && (
          <button
            onClick={onRegenerate}
            disabled={regenerating}
            style={{
              fontSize: 11, padding: '4px 10px', borderRadius: 99,
              border: '1px solid #3B82F6', background: 'transparent',
              color: regenerating ? '#6B7280' : '#93C5FD',
              cursor: regenerating ? 'default' : 'pointer', fontWeight: 700,
            }}
          >
            {regenerating ? '重算中…' : '🔄 重新生成方案'}
          </button>
        )}
      </div>

      <div style={{ fontSize: 12, color: '#D1D5DB', whiteSpace: 'pre-line', lineHeight: 1.7 }}>
        {fallback?.message || '找不到同时满足这些条件的商品。'}
      </div>

      {/* 检索证据：检索词 / 数据源 / 候选规模，让"无解"可被核对 */}
      {(retrieval?.search_query || retrieval?.source) && (
        <div style={{ marginTop: 8, fontSize: 11, color: '#6B7280', display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {retrieval?.search_query && <span>实际检索词「{retrieval.search_query}」</span>}
          {retrieval?.source && <span>数据源 {dataSourceLabel(retrieval.source)}</span>}
          {typeof retrieval?.candidate_count === 'number' && <span>召回 {retrieval.candidate_count} 件</span>}
          {typeof retrieval?.matched_count === 'number' && <span>命中 {retrieval.matched_count} 件</span>}
          {typeof retrieval?.filtered_count === 'number' && retrieval.filtered_count > 0 && (
            <span>过滤 {retrieval.filtered_count} 件</span>
          )}
          {channel3Down && <span style={{ color: '#F87171' }}>赞助商检索暂时不可用（已降级）</span>}
        </div>
      )}

      {satisfaction && (
        <div style={{ marginTop: 12 }}>
          <SatisfactionBlock satisfaction={satisfaction} scope={scope} />
        </div>
      )}

      {suggestions.length > 0 && (
        <div style={{ marginTop: 6 }}>
          <div style={{ fontSize: 11, color: '#93C5FD', fontWeight: 700, marginBottom: 4 }}>可以试试</div>
          {suggestions.map((s, i) => (
            <div key={i} style={{ fontSize: 11, color: '#D1D5DB', lineHeight: 1.6 }}>
              · <b>{s.action}</b>：{s.detail}
              {s.estimated_impact ? `（${s.estimated_impact}）` : ''}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// 约束判定块：组合级 N/M + 逐条 ✅/❌/❔，每条都带可复算证据串。
// 红线：unknown 绝不显示成 ✅；❌ 默认最多 2 条（后端已折叠，这里只提示还有几条）。
// scope='catalog' 表示这份判定是在**候选池全量**上跑的（无解场景的"为什么无解"）。
function SatisfactionBlock({ satisfaction, scope }: { satisfaction?: Satisfaction; scope?: string }) {
  const catalogScope = scope === 'catalog'
  if (!satisfaction || !satisfaction.judgeable) {
    return (
      <div style={{ marginBottom: 10, fontSize: 10, color: '#6B7280', fontStyle: 'italic' }}>
        {catalogScope
          ? '本次需求没有可自动判定的约束，无法用商品数据解释"为什么无解"——请人工核对商品目录。'
          : '本次需求没有可自动判定的硬约束（预算/环保/品类等），下方商品按检索相关度给出。'}
      </div>
    )
  }
  const iconOf = (s: VerdictStatus): { icon: string; color: string } =>
    s === 'sat' ? { icon: '✅', color: '#6EE7B7' }
      : s === 'unsat' ? { icon: '❌', color: '#FCA5A5' }
        : { icon: '❔', color: '#FCD34D' }
  const shown = (satisfaction.constraints || []).filter(c => c.visible !== false)
  const hidden = satisfaction.hidden_failures || []

  return (
    <div style={{ marginBottom: 10, background: '#0b1220', border: '1px solid #374151', borderRadius: 6, padding: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: '#93C5FD' }}>
          📋 约束判定{catalogScope ? '（候选池覆盖）' : ''}
        </span>
        <span style={{ fontSize: 10, color: satisfaction.failed > 0 ? '#FCD34D' : '#6EE7B7', fontWeight: 600 }}>
          {satisfaction.headline}
        </span>
      </div>
      {shown.map(c => {
        const { icon, color } = iconOf(c.status)
        return (
          <div key={c.constraint_id} style={{ display: 'flex', gap: 6, fontSize: 10, lineHeight: 1.5, marginBottom: 2 }}>
            <span style={{ color, flexShrink: 0 }}>{icon}</span>
            <span style={{ minWidth: 0 }}>
              <span style={{ color: '#D1D5DB' }}>{c.content || c.constraint_id}</span>
              {(c.evidence || c.reason) && (
                <span style={{ color: '#6B7280' }}>{c.evidence || c.reason}</span>
              )}
            </span>
          </div>
        )
      })}
      {hidden.length > 0 && (
        <div style={{ fontSize: 10, color: '#6B7280', marginTop: 3 }}>
          另有 {hidden.length} 条不满足已折叠（数据完整保留，未隐藏计算）
        </div>
      )}
      {satisfaction.unknown > 0 && (
        <div style={{ fontSize: 10, color: '#6B7280', marginTop: 3 }}>
          ❔ {satisfaction.unknown} 条无法从商品数据判定（不当作满足）
        </div>
      )}
    </div>
  )
}

// 二次确认弹窗（带金额的"确认下单"按钮）
function SplitConfirmationModal({ sessionId, plan, mode, onClose }: {
  plan: Plan
  sessionId: string
  /** 单人模式下没有"分摊给成员"这回事：不显示每人应付/尾差。 */
  mode?: 'single' | 'group' | null
  onClose: () => void
}) {
  const total = plan.total ?? 0
  const itemCount = plan.items?.length || 0
  const isGroup = mode === 'group'
  // 购物车模式：plan 是「伪 Plan」，标题/订单名都用购物车语义，不冒充"方案 A"
  const isCartMode = /当前购物车/.test(plan.strategy || '')
  const planLabel = isCartMode ? '当前购物车' : `方案 ${plan.plan_id}: ${plan.strategy || ''}`
  const [preview, setPreview] = useState<SplitPreviewResponse | null>(null)
  const [previewLoading, setPreviewLoading] = useState(true)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [modalError, setModalError] = useState<string | null>(null)
  const [confirmDisabled, setConfirmDisabled] = useState(false)
  // 下单金额以**购物车**为准：预算拦截后可先改单，再在同一个弹窗里确认（服务端本来也只认购物车总价）
  const [cartTotal, setCartTotal] = useState<number>(total)
  const [cartCount, setCartCount] = useState<number>(itemCount)
  // 改单示例必须来自**本方案真实商品**：写死「木质相框、铝制奖牌」会让用户以为
  // 系统在推荐别的东西（发黑胶唱片却提示删相框），这正是"跟我的提问无关"的来源。
  const editExample = plan.items?.[0]?.name
    ? `删掉 ${plan.items[0].name}`
    : '把最贵的一件去掉'
  const [editInstruction, setEditInstruction] = useState('')
  const [cartEditing, setCartEditing] = useState(false)
  const [editNote, setEditNote] = useState<string | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

  useEffect(() => {
    // 单人模式下没有分摊，跳过 fetch（避免单人订单里冒出"尾差由成员 E 承担"这类残留）
    if (!isGroup) {
      setPreviewLoading(false)
      return
    }
    let alive = true
    fetchSplitPreview(sessionId)
      .then(res => { if (alive) setPreview(res) })
      .catch(err => {
        if (!alive) return
        setPreviewError(err instanceof Error ? err.message : '分摊预览获取失败')
      })
      .finally(() => { if (alive) setPreviewLoading(false) })
    return () => { alive = false }
  }, [sessionId, isGroup])

  // 金额以**真实购物车**为准：打开弹窗先同步一次 /cart，避免弹窗金额和购物车面板对不上。
  // 失败不阻塞下单（服务端仍会按购物车校验），只是沿用方案快照金额。
  useEffect(() => {
    let alive = true
    fetchCart(sessionId)
      .then(res => {
        if (!alive) return
        if (typeof res.total === 'number') setCartTotal(res.total)
        if (typeof res.items?.length === 'number') setCartCount(res.items.length)
      })
      .catch(() => { /* 忽略：沿用快照金额 */ })
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
        item_name: isCartMode ? 'Cart Checkout' : `Group Purchase Plan ${plan.plan_id}`,
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
            {planLabel} · {cartCount} 件商品
          </div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#10B981' }}>总计 {fmtUsd(cartTotal)}</div>
          {Math.abs(cartTotal - total) > 0.005 && (
            <div style={{ fontSize: 10, color: '#9CA3AF', marginTop: 2 }}>
              方案快照 {fmtUsd(total)} → 购物车改单后 {fmtUsd(cartTotal)}
            </div>
          )}
        </div>

        {/* === F2 分摊预览（仅团购）：弹窗打开时 fetch，loading 禁用确认按钮 ===
            单人模式没有成员可分，显示「每人应付 / 尾差由成员 E 承担」纯属误导。 */}
        {isGroup && (
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
        )}

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
            🛒 购物车改单（可选）：例如「{editExample}」
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

        <div style={{ marginTop: 12, fontSize: 10, color: '#6B7280', lineHeight: 1.5 }}>
          商品库存与价格以检索时点及商家页为准；最终金额以 PayPal 结算页为准，本工具不自动发起支付。
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

/** 方案卡片的预算上限：只认真实预算约束，缺失时返回 null（不要伪造 600）。 */
function getBudgetFromPlan(plan: Plan): number | null {
  const limit = plan.budget_check?.limit
  return typeof limit === 'number' && limit > 0 ? limit : null
}