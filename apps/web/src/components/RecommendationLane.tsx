import { useEffect, useState } from 'react'
import {
  addRecommendation,
  fetchRecommendations,
  type Plan,
  type RecommendationCard,
  type RecommendationsResponse,
} from '../api'

/**
 * ✨ 第三条泳道：Channel3 推荐（来自「搜过 / 选过」的事实，不参与上方 A/B 对比）。
 *
 * 为什么单独一条泳道而不是并进方案：
 *   A/B 必须能被评委拿同一句需求复算（同一候选池 + 两个目标函数）；一旦把历史信号
 *   混进候选池，同样的需求会因为「昨天搜过什么」跑出不同方案，对比就不可复核了。
 *   所以记忆只在这里出现 —— 这条泳道就是 A/B 刻意不用、但用户确实拥有的第二层信息。
 *
 * 每张卡都带三段式出处：你搜过「X」→ 本次检索「query_used」→ 命中 N 件，展示这几件。
 * query_used 可以直接拿去 Channel3 复搜，出处不是装饰文字。
 */
type Props = {
  sessionId: string
  planA?: Plan | null
  planB?: Plan | null
  refreshKey?: number
}

const norm = (s: string): string => s.toLowerCase().replace(/\s+/g, ' ').trim()

const cardStyle: React.CSSProperties = {
  background: '#111827',
  border: '1px solid #4C1D95',
  borderRadius: 8,
  padding: 8,
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  minWidth: 0,
}

export function RecommendationLane({ sessionId, planA, planB, refreshKey = 0 }: Props) {
  const [data, setData] = useState<RecommendationsResponse | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'warn'; text: string } | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let alive = true
    fetchRecommendations(sessionId, 3)
      .then(res => {
        if (alive) setData(res)
      })
      .catch(() => {
        /* 可选增强：后端未升级 / 离线时静默隐藏，不打断方案主流程 */
      })
    return () => {
      alive = false
    }
  }, [sessionId, refreshKey, reloadKey])

  if (!data) return null
  if (data.channel3?.status === 'no_signal') return null

  const namesA = new Set((planA?.items || []).map(i => norm(i.name)))
  const namesB = new Set((planB?.items || []).map(i => norm(i.name)))
  const inPlan = (name: string): string => {
    const n = norm(name)
    if (namesA.has(n) && namesB.has(n)) return '已在方案 A/B'
    if (namesA.has(n)) return '已在方案 A'
    if (namesB.has(n)) return '已在方案 B'
    return '两方案都没有'
  }

  const handleAdd = async (c: RecommendationCard) => {
    setBusy(c.id)
    setMsg(null)
    try {
      const res = await addRecommendation(sessionId, c.id)
      if (res.success) {
        setMsg({ kind: 'ok', text: `已加入购物车：${c.name}（购物车合计 $${(res.cart?.total ?? 0).toFixed(2)}）` })
      } else if (res.code === 'budget_blocked') {
        setMsg({ kind: 'warn', text: res.message || '预算守卫拦截：购物车未改动' })
      } else {
        setMsg({ kind: 'warn', text: res.message || '加购失败' })
      }
    } catch (err) {
      setMsg({ kind: 'warn', text: err instanceof Error ? err.message : '加购失败' })
    } finally {
      setBusy(null)
    }
  }

  const signals = data.signals || {}
  const topSearch = (signals.recent_searches || [])[0]

  return (
    <div style={{ border: '1px solid #4C1D9566', borderRadius: 8, padding: 8, marginBottom: 12, background: '#0f0a1a' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 4 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: '#C4B5FD' }}>
          ✨ 第三条泳道 · Channel3 推荐
        </span>
        <span style={{ fontSize: 10, color: '#6B7280' }}>
          来自检索记忆，不参与上方 A/B 对比（对比只用本次需求，才可复算）
        </span>
        <button
          onClick={() => setReloadKey(k => k + 1)}
          title="重新按当前历史检索一次"
          style={{ marginLeft: 'auto', background: 'none', border: '1px solid #4C1D95', color: '#A78BFA', borderRadius: 99, fontSize: 10, padding: '1px 8px', cursor: 'pointer' }}
        >
          重新检索
        </button>
      </div>

      <div style={{ fontSize: 10, color: '#9CA3AF', lineHeight: 1.5, marginBottom: 6 }}>
        {topSearch && (
          <div>
            你搜过「{topSearch.query}」{topSearch.count ?? 1} 次
            {signals.last_selection?.plan_id && `　·　上次选过 方案 ${signals.last_selection.plan_id}`}
          </div>
        )}
        <div style={{ color: '#6B7280' }}>
          本次检索「{data.query_used}」→ 命中 {data.search_hits} 件，展示 {data.shown} 件
          {data.channel3?.status !== 'ok' && `（Channel3 状态：${data.channel3?.status}）`}
        </div>
      </div>

      {data.items.length === 0 ? (
        <div style={{ fontSize: 10, color: '#6B7280', fontStyle: 'italic' }}>
          {data.note || '这次没有可展示的 Channel3 推荐。'}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 8 }}>
          {data.items.map(c => (
            <div key={c.id} style={cardStyle}>
              <div style={{ fontSize: 11, color: '#E5E7EB', fontWeight: 600, lineHeight: 1.3, maxHeight: 30, overflow: 'hidden' }}>
                {c.name}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 12, color: '#6EE7B7', fontWeight: 700 }}>${c.price.toFixed(2)}</span>
                <span
                  style={{
                    fontSize: 9,
                    padding: '1px 6px',
                    borderRadius: 99,
                    color: inPlan(c.name) === '两方案都没有' ? '#C4B5FD' : '#34D399',
                    background: inPlan(c.name) === '两方案都没有' ? '#2e1065' : '#052e16',
                  }}
                >
                  {inPlan(c.name)}
                </span>
              </div>
              <div style={{ fontSize: 9, color: '#9CA3AF', lineHeight: 1.4, maxHeight: 26, overflow: 'hidden' }}>
                {c.reason}
              </div>
              <button
                onClick={() => handleAdd(c)}
                disabled={busy !== null}
                style={{
                  marginTop: 'auto',
                  fontSize: 10,
                  padding: '2px 0',
                  borderRadius: 6,
                  border: '1px solid #4C1D95',
                  background: busy === c.id ? '#1f2937' : '#1e1b4b',
                  color: '#C4B5FD',
                  cursor: busy !== null ? 'default' : 'pointer',
                }}
              >
                {busy === c.id ? '加购中…' : '加入购物车'}
              </button>
            </div>
          ))}
        </div>
      )}

      {msg && (
        <div style={{ marginTop: 6, fontSize: 10, color: msg.kind === 'ok' ? '#34D399' : '#FBBF24' }}>
          {msg.kind === 'ok' ? '✅' : '⚠️'} {msg.text}
        </div>
      )}
    </div>
  )
}

