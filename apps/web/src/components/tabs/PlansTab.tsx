import { useEffect, useMemo, useState } from 'react'
import {
  fetchPlans,
  regeneratePlans,
  type Plan,
  type PlanItem,
  type PlansComparison,
  type PlansResponse,
} from '../../api'
import { fetchSplitPreview, type SplitPreviewResponse, createOrder, modifyCart } from '../../api'
import { RecommendationLane } from '../RecommendationLane'

type Props = {
  sessionId: string
  onSelectPlan: (planId: 'A' | 'B') => Promise<void>
  selectedPlanId: 'A' | 'B' | null
  /** intent 提交后 ++，触发本 Tab 立即重拉方案（非轮询等待）。 */
  refreshKey?: number
  intentQuery?: string | null
  intentMode?: 'single' | 'group' | null
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

export function PlansTab({
  sessionId,
  onSelectPlan,
  selectedPlanId,
  refreshKey = 0,
  intentQuery,
  intentMode,
}: Props) {
  const [plans, setPlans] = useState<{ planA: Plan | null; planB: Plan | null }>({ planA: null, planB: null })
  const [comparison, setComparison] = useState<PlansComparison | null>(null)
  const [retrieval, setRetrieval] = useState<PlansResponse['retrieval']>(undefined)
  const [serverIntent, setServerIntent] = useState<{ query: string; mode: 'single' | 'group' } | null>(null)
  const [loading, setLoading] = useState(true)
  const [showingConfirm, setShowingConfirm] = useState<'A' | 'B' | null>(null)
  const [selecting, setSelecting] = useState<'A' | 'B' | null>(null)
  const [cardError, setCardError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)

  // 检索依据：优先用后端回显（snapshot 重放时也有），不然用刚提交的提问原文
  const effectiveQuery = retrieval?.query || serverIntent?.query || intentQuery || ''
  const effectiveMode = serverIntent?.mode || intentMode || null

  // 初始加载 + 自适应轮询（等新方案时 1.5s，稳定后 5s）+ intent 提交后立即刷新
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    let tries = 0

    // 新检索词 = 新任务：先把上一问的卡片清掉并进入"生成中"。
    // 否则旧方案会一直挂在屏幕上，用户看到的就是"换了问题还是旧推荐"。
    setPlans({ planA: null, planB: null })
    setComparison(null)
    setRetrieval(undefined)
    setLoading(true)
    tries = 0

    /**
     * 落地前先校验快照是不是**本次提问**的：
     * 提交新需求时，上一轮（带 Channel3 检索，可能几秒到几十秒）仍可能在快照失效后落盘，
     * GET 就会把上一问的方案回放出来——表现就是"换了问题却还是旧方案，要手动刷新"。
     * 所以查询词对不上就不落地，按 1.5s 快跑重试，新快照一到立刻替换。
     */
    const apply = (res: PlansResponse): boolean => {
      const respQuery = res.retrieval?.query
      if (intentQuery && respQuery && respQuery !== intentQuery) return false
      setPlans(plansFrom(res))
      setComparison(res.comparison || null)
      setRetrieval(res.retrieval)
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
        setPlans(plansFrom(res))
        setComparison(res.comparison || null)
        setRetrieval(res.retrieval)
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
        {selectedPlanId ? (
          <button
            onClick={() => setShowingConfirm(selectedPlanId)}
            style={{ fontSize: 11, padding: '4px 10px', borderRadius: 99, border: '1px solid #10B981', background: 'transparent', color: '#6EE7B7', cursor: 'pointer', fontWeight: 700 }}
          >
            💳 确认并支付方案 {selectedPlanId}
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
          {retrieval?.source && <span style={{ color: '#9CA3AF' }}>数据源 {retrieval.source}</span>}
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
              候选 {retrieval.candidate_count} 件 · 命中 {retrieval.matched_count ?? 0} 件
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

      {/* Diff 视图：突出两方案差异项 */}
      {plans.planA && plans.planB && (
        <DiffView planA={plans.planA} planB={plans.planB} comparison={comparison} />
      )}

      {/* 第三条泳道：历史信号只在这里出现（A/B 刻意不用，才能被同一句需求复算） */}
      <RecommendationLane
        sessionId={sessionId}
        planA={plans.planA}
        planB={plans.planB}
        refreshKey={refreshKey}
      />

      {/* 双方案对比卡片（左右布局） */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12, alignItems: 'start' }}>
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
          mode={effectiveMode}
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
  // 共有/专属件数以后端 comparison 为准（B 是 A 的同品类升级，共有商品必然 > 0；
  // 前端 computeDiff 只作为旧数据兜底，避免显示 "A 8 · 共有 0 · B 8" 这种自相矛盾）
  const sharedCount = comparison?.shared_count ?? common.length
  const onlyACount = comparison?.only_a_count ?? onlyA.length
  const onlyBCount = comparison?.only_b_count ?? onlyB.length
  const upgrades = comparison?.upgrades || []
  // 逐件合计 vs 总价差：能对上就明说「可逐条核对」，对不上就老实说差额来自件数不同，
  // 不让 UI 出现「合计 +$17、总价差 +$12」这种自相矛盾的数字。
  const upgradeSum = round2(upgrades.reduce((s, u) => s + (u.delta || 0), 0))
  const reconciled = Math.abs(upgradeSum - delta) < 0.01
  const [open, setOpen] = useState(true)

  return (
    <div style={{ border: '1px dashed #60A5FA66', borderRadius: 8, padding: 10, marginBottom: 12, background: '#0d1526' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontSize: 12, color: '#93C5FD', fontWeight: 700 }}
      >
        ⚖️ 差异对比
        <span style={{ fontSize: 11, color: '#9CA3AF', fontWeight: 500 }}>
          A 专属 {onlyACount} · 共有 {sharedCount} · B 专属 {onlyBCount}
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
          {/* 叙述层：B 不是一个"更贵的方案"，而是一次具体的替换操作。
              先看换了哪几件、合计多少；价格差退成第二行做核对 —— 评委看到的是决策，不是差价。 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11, color: '#9CA3AF' }}>
              {upgrades.length > 0 ? (
                <>
                  B 换了 <b style={{ color: '#FBBF24' }}>{upgrades.length} 件</b>（合计{' '}
                  <b style={{ color: '#FBBF24' }}>+{fmtUsd(upgradeSum)}</b>）
                  <span style={{ color: '#6B7280' }}>
                    　总价 A {fmtUsd(planA.total || 0)} → B {fmtUsd(planB.total || 0)}（差{' '}
                    {delta >= 0 ? '+' : ''}{fmtUsd(delta)}
                    {reconciled ? '，逐件合计 = 总价差，可逐条核对' : '，差额来自件数不同'}）
                  </span>
                </>
              ) : (
                <>
                  两方案件件相同（{fmtUsd(planA.total || 0)}）
                  <span style={{ color: '#6B7280' }}>
                    　B 的同品类里没有匹配分更高的候选，所以升级为空（不是 B 更差，是本次候选池没有可换的东西）
                  </span>
                </>
              )}
            </span>
            {recommendation && (
              <span style={{ fontSize: 11, color: '#C77DFF', background: '#1e1b4b', padding: '2px 8px', borderRadius: 99 }}>
                💡 {recommendation}
              </span>
            )}
          </div>

          {/* 逐件 diff 放在三栏之前：B 的每一件都是 A 同品类里更贵/更好的选择，
              价差可逐件归因、也可以否决（说明 B 只贵在"这几件"上，不是整体乱配） */}
          {upgrades.length > 0 && (
            <div style={{ marginTop: 8, background: '#111827', borderRadius: 6, padding: 6 }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: '#FBBF24', marginBottom: 4 }}>
                ⬆️ 逐件替换（{upgrades.length} 项，合计 {fmtUsd(upgradeSum)}）
              </div>
              {upgrades.map((u, idx) => (
                <div key={idx} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 10, color: '#D1D5DB', marginBottom: 2 }}>
                  <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {u.item ? `${u.item} → ${u.to_item || '同品类升级'}` : '同品类升级'}
                    {u.category ? `　[${u.category}]` : ''}
                  </span>
                  <span style={{ color: '#FBBF24', flexShrink: 0 }}>
                    {fmtUsd(u.from)} → {fmtUsd(u.to)}（+{fmtUsd(u.delta)}）
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* 对比标准（可复核）：把「凭什么这样比」写进 UI，而不是只给结论。
              同一候选池 + 两个目标函数（最低总价 / 最高关键词匹配分）+ 帕累托判定。 */}
          {comparison?.criteria && (
            <div style={{ marginTop: 8, fontSize: 10, color: '#9CA3AF', background: '#0b1220', borderRadius: 6, padding: 6, lineHeight: 1.55 }}>
              <div style={{ color: '#93C5FD', fontWeight: 700, marginBottom: 2 }}>📐 对比标准（可复核）</div>
              <div>{comparison.criteria.domain}</div>
              <div>A：{comparison.criteria.objective_a}　·　B：{comparison.criteria.objective_b}</div>
              <div style={{ color: '#6B7280' }}>
                {comparison.criteria.same_feasibility}　·　{comparison.criteria.evidence}
              </div>
              <div style={{ color: '#6B7280' }}>
                A/B 只用本次需求的两个可复算目标，<b style={{ color: '#A78BFA' }}>不使用检索记忆</b> —— 记忆只在下方「第三条泳道」出现。
              </div>
            </div>
          )}
          {comparison?.pareto?.note && (
            <div style={{ marginTop: 6, fontSize: 10, color: comparison.pareto.relation === 'trade_off' ? '#FCD34D' : '#F87171' }}>
              {comparison.pareto.relation === 'trade_off' ? '⚖️' : '⚠️'} {comparison.pareto.note}
              {typeof comparison.metrics?.relevance_a === 'number' && (
                <span style={{ color: '#6B7280' }}>
                  {' '}（匹配分 A {comparison.metrics.relevance_a} / B {comparison.metrics.relevance_b}）
                </span>
              )}
            </div>
          )}


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
          <div key={item.id || idx} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10, color: '#D1D5DB', marginBottom: 2, overflow: 'hidden' }}>
            {item.image_url ? (
              <img
                src={item.image_url}
                alt={item.name}
                loading="lazy"
                style={{ width: 18, height: 18, borderRadius: 3, objectFit: 'cover', flexShrink: 0, background: '#1f2937' }}
                onError={e => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
              />
            ) : (
              <span style={{ width: 18, flexShrink: 0 }} />
            )}
            <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {item.name}
            </span>
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
  budgetLimit: number | null
  onSelect: () => void
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
              {item.quantity > 1 && (
                <span style={{ color: '#FBBF24', marginLeft: 4, fontSize: 10 }}>×{item.quantity}</span>
              )}
            </span>
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