import { useEffect, useState } from 'react'
import {
  fetchSearchMemory,
  type SearchMemoryEntry,
  type SelectionMemoryEntry,
} from '../api'

/**
 * 🧠 检索记忆条：把「搜过什么 / 选过什么」变成一键再搜的入口。
 *
 * 诚实边界：只回显历史事实，**不会**把历史检索词混进本次 Channel3 查询；
 * 点一下等于把当时那句话重新提交一次（约束与方案都重新生成）。
 */
type Props = {
  sessionId: string
  onPick: (query: string) => void
  refreshKey?: number
}

const chip: React.CSSProperties = {
  maxWidth: 260,
  padding: '3px 10px',
  borderRadius: 99,
  border: '1px solid #374151',
  background: '#111827',
  color: '#9CA3AF',
  fontSize: 11,
  cursor: 'pointer',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

export function SearchMemoryBar({ sessionId, onPick, refreshKey }: Props) {
  const [searches, setSearches] = useState<SearchMemoryEntry[]>([])
  const [selections, setSelections] = useState<SelectionMemoryEntry[]>([])

  useEffect(() => {
    let alive = true
    fetchSearchMemory(8)
      .then(res => {
        if (!alive) return
        setSearches(res.recent_searches || [])
        setSelections(res.recent_selections || [])
      })
      .catch(() => {
        /* 记忆是可选增强：后端未升级 / 离线时静默隐藏，不打断主流程 */
      })
    return () => {
      alive = false
    }
  }, [sessionId, refreshKey])

  if (searches.length === 0 && selections.length === 0) return null

  const top = searches.slice(0, 4)
  const lastPick = selections[0]

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, margin: '0 0 14px' }}>
      <span style={{ fontSize: 11, color: '#6B7280' }}>🧠 检索记忆</span>
      {top.map(s => (
        <button
          key={`${s.query}-${s.ts || ''}`}
          onClick={() => onPick(s.query)}
          title={`再搜一次（搜过 ${s.count ?? 1} 次）`}
          style={chip}
        >
          {s.query.length > 20 ? `${s.query.slice(0, 20)}…` : s.query}
        </button>
      ))}
      {lastPick?.query && (
        <button
          onClick={() => onPick(lastPick.query as string)}
          title={`当时的购物车：${(lastPick.items || []).join(' / ') || '（无明细）'}`}
          style={{ ...chip, borderColor: '#34D39966', color: '#34D399' }}
        >
          上次选过 方案 {lastPick.plan_id} · ${(lastPick.total ?? 0).toFixed(2)} · {lastPick.item_count ?? 0} 件
        </button>
      )}
    </div>
  )
}
