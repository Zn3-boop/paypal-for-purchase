import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import type { ConstraintItem, ConflictItem } from '../../api'
import { extractConstraints, fetchConstraints, deleteConstraint } from '../../api'
import { ADD_TRIGGER, DELETE_TRIGGER, isActive, matchDeleteTargets } from '../../utils/constraintDelete'

type Props = {
  sessionId: string
}

export function ConstraintsDashboard({ sessionId }: Props) {
  const [constraints, setConstraints] = useState<ConstraintItem[]>([])
  const [conflicts, setConflicts] = useState<ConflictItem[]>([])
  const [loading, setLoading] = useState(true)
  const [input, setInput] = useState('')
  const [extracting, setExtracting] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteCandidates, setDeleteCandidates] = useState<ConstraintItem[]>([])
  const [feedback, setFeedback] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  // 看板默认折叠：约束照旧在后端生效（支付守卫仍会复核），只是不再占满首屏、
  // 把「方案对比」挤到下面去。需要加/删约束时展开即可。
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!sessionId) return
    
    fetchConstraints(sessionId)
      .then(res => {
        setConstraints(res.constraints?.filter(isActive) || [])
        setConflicts(res.conflicts || [])
      })
      .catch(err => {
        console.error('Failed to fetch constraints:', err)
      })
      .finally(() => setLoading(false))
  }, [sessionId])

  // Polling every 3 seconds to keep dashboard fresh
  useEffect(() => {
    const interval = setInterval(() => {
      if (sessionId) {
        fetchConstraints(sessionId)
          .then(res => {
            setConstraints(res.constraints?.filter(isActive) || [])
            setConflicts(res.conflicts || [])
          })
      }
    }, 3000)
    return () => clearInterval(interval)
  }, [sessionId])

  // 随写随显：App 在 intent 提交成功后派发 ecv:constraints-changed，
  // 这里立即重拉，不等 3s 轮询（否则会出现“共 0 条”/“共 1 条”不同步）
  useEffect(() => {
    const reload = () => {
      if (!sessionId) return
      fetchConstraints(sessionId)
        .then(res => {
          setConstraints(res.constraints?.filter(isActive) || [])
          setConflicts(res.conflicts || [])
        })
    }
    window.addEventListener('ecv:constraints-changed', reload)
    return () => window.removeEventListener('ecv:constraints-changed', reload)
  }, [sessionId])

  /** A5：新增走 /constraints/extract；删除只做本地匹配 + DELETE（LLM 解析通常 1-3 秒）。 */
  const handleExtract = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || extracting) return
    setFeedback(null)
    setDeleteCandidates([])

    // 边界 1：删除型指令绝不进入 extract（避免“删掉预算 600”被解析成新增预算 600）。
    if (DELETE_TRIGGER.test(text)) {
      // 边界 2：删除 + 新增混合指令不做部分执行。
      if (ADD_TRIGGER.test(text)) {
        setFeedback({ kind: 'err', text: '一次只处理一条指令：请先只删除，或只新增。' })
        return
      }
      const targets = matchDeleteTargets(text, constraints)
      if (targets.length === 0) {
        setFeedback({ kind: 'err', text: '未找到匹配的约束。可直接点卡片右上角 × 删除。' })
        return
      }
      if (targets.length > 1) {
        setDeleteCandidates(targets)
        setFeedback({ kind: 'err', text: `匹配到 ${targets.length} 条约束，请在下方候选中点选要删除的一条。` })
        return
      }
      await handleDeleteOne(targets[0])
      return
    }

    setExtracting(true)
    try {
      const res = await extractConstraints(sessionId, text)
      if (!res.ok) {
        setFeedback({ kind: 'err', text: res.message || '未能提取到任何约束' })
      } else {
        setFeedback({ kind: 'ok', text: `已添加 ${res.extracted_count} 条约束` })
        setInput('')
        if (res.constraints) setConstraints(prev => [...prev, ...res.constraints!.filter(isActive)])
        if (res.conflicts) setConflicts(res.conflicts)
      }
    } catch (err) {
      setFeedback({ kind: 'err', text: err instanceof Error ? err.message : '约束解析失败，请稍后再试' })
    } finally {
      setExtracting(false)
    }
  }

  /** 唯一删除路径：后端软删除 + 本地同步移除；审计由 DELETE 端点写 constraint_changed(removed)。 */
  const handleDeleteOne = async (constraint: ConstraintItem) => {
    if (!constraint.id || deletingId) return
    setDeletingId(constraint.id)
    setFeedback(null)
    try {
      await deleteConstraint(sessionId, constraint.id)
      setConstraints(prev => prev.filter(c => c.id !== constraint.id))
      setDeleteCandidates([])
      setFeedback({ kind: 'ok', text: `已删除约束：${constraint.content || constraint.type}` })
    } catch (err) {
      setFeedback({ kind: 'err', text: err instanceof Error ? err.message : '删除约束失败，请稍后再试' })
    } finally {
      setDeletingId(null)
    }
  }

  if (loading && constraints.length === 0) {
    return (
      <div style={{ padding: 16, textAlign: 'center', color: '#9CA3AF', fontSize: 13 }}>
        加载约束中...
      </div>
    )
  }

  const hasConflicts = conflicts.length > 0

  return (
    <div style={{ background: '#0f172a', borderRadius: 12, padding: 16, border: hasConflicts ? '1px solid #EF4444' : '1px solid #374151' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ 
            width: 10, 
            height: 10, 
            borderRadius: '50%', 
            background: hasConflicts ? '#EF4444' : '#10B981',
            animation: hasConflicts ? 'pulse 1s infinite' : 'none'
          }} />
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: hasConflicts ? '#EF4444' : '#10B981' }}>
            {hasConflicts ? '⚠️ 存在约束冲突' : '✅ 所有约束满足'}
          </h3>
        </div>
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          style={{
            fontSize: 11, color: '#93C5FD', background: 'transparent',
            border: '1px solid #3B82F655', borderRadius: 99, padding: '3px 10px', cursor: 'pointer',
          }}
        >
          共 {constraints.length} 条 · {expanded ? '收起 ▴' : '展开 ▾'}
        </button>
      </div>

      {/* 折叠态也保留一行摘要：约束在哪、是否还生效，不该让人猜 */}
      {!expanded && (
        <div style={{ fontSize: 11, color: '#6B7280' }}>
          {constraints.length === 0
            ? '尚未提取到约束 —— 展开可手动添加（预算、人数、忌口…）'
            : `本任务内生效 · 支付时守卫复核：${constraints.slice(0, 3).map(c => c.content || c.type).join(' / ')}${constraints.length > 3 ? ' …' : ''}`}
        </div>
      )}

      {expanded && (
      <>

      {/* 生命周期一句话就够：会话级 + 下次提问替换自动抽取的那批，手动添加保留 */}
      <div style={{ fontSize: 10, color: '#6B7280', marginBottom: 8 }}>
        再次提问会替换上次自动抽取的约束；你手动添加的会保留。
      </div>

      {/* A5: 自然语言添加约束（POST /constraints/extract，Enter 或按钮触发） */}
      <form onSubmit={handleExtract} style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          disabled={extracting}
          placeholder="成员A说总预算 600 美元"
          aria-label="自然语言约束输入"
          style={{ flex: 1, minWidth: 0, padding: '8px 10px', borderRadius: 6, border: '1px solid #374151', background: '#111827', color: '#E5E7EB', fontSize: 12 }}
        />
        <button
          type="submit"
          disabled={extracting || !input.trim()}
          style={{ padding: '8px 14px', borderRadius: 6, border: 'none', background: '#3B82F6', color: '#fff', fontWeight: 600, fontSize: 12, cursor: (extracting || !input.trim()) ? 'default' : 'pointer', opacity: (extracting || !input.trim()) ? 0.6 : 1 }}
        >
          {extracting ? '解析中…' : '添加约束'}
        </button>
      </form>
      {extracting && (
        <div style={{ fontSize: 11, color: '#93C5FD', marginBottom: 10 }}>LLM 正在解析约束（通常 1-3 秒）…</div>
      )}
      {feedback && (
        <div style={{ fontSize: 11, marginBottom: 10, color: feedback.kind === 'ok' ? '#6EE7B7' : '#FCA5A5' }}>
          {feedback.kind === 'ok' ? '✓ ' : '⚠️ '}{feedback.text}
        </div>
      )}

      {deleteCandidates.length > 0 && (
        <div style={{ marginBottom: 10, padding: 8, borderRadius: 6, background: '#1f2937', border: '1px solid #F59E0B55' }}>
          <div style={{ fontSize: 11, color: '#FCD34D', marginBottom: 6 }}>请选择要删除的约束：</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {deleteCandidates.map(candidate => (
              <button
                key={candidate.id || candidate.content}
                type="button"
                disabled={deletingId !== null}
                onClick={() => void handleDeleteOne(candidate)}
                style={{ padding: '5px 8px', borderRadius: 5, border: '1px solid #F59E0B88', background: '#1a1600', color: '#FDE68A', fontSize: 11, cursor: deletingId ? 'default' : 'pointer' }}
              >
                {candidate.content || candidate.type}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Conflicts Section (if any) */}
      {hasConflicts && (
        <div style={{ marginBottom: 12, background: '#2a0d0d', border: '1px solid #EF444444', borderRadius: 8, padding: 10 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: '#F87171', marginBottom: 6 }}>
            🔥 冲突检测 ({conflicts.length})
          </div>
          {conflicts.map((c, idx) => (
            <div key={idx} style={{ fontSize: 12, color: '#FCA5A5', marginBottom: 4 }}>
              <span style={{ color: '#EF4444' }}>✗</span> {c.detail}
            </div>
          ))}
        </div>
      )}

      {/* Constraints List */}
      <div style={{ display: 'grid', gap: 8 }}>
        {constraints.map((c, idx) => (
          <ConstraintItem
            key={c.id || `${c.type}-${idx}`}
            constraint={c}
            onDelete={() => void handleDeleteOne(c)}
            deleting={deletingId === c.id}
          />
        ))}
        {constraints.length === 0 && (
          <div style={{ fontSize: 12, color: '#6B7280', fontStyle: 'italic', textAlign: 'center', padding: 16 }}>
            暂无约束，请在上方输入购物需求
          </div>
        )}
      </div>

      {/* Footer Stats 已删：功能/预算/偏好按类型计数对用户决策没有信息量，
          真正有用的是上面的约束卡片本身和方案里的守卫结论。 */}
      </>
      )}
    </div>
  )
}

// Individual constraint card
function ConstraintItem({ constraint, onDelete, deleting }: {
  constraint: ConstraintItem
  onDelete: () => void
  deleting: boolean
}) {
  const isUserSource = constraint.source === 'user'
  const isLLMSource = constraint.source === 'llm'
  
  // Format numeric values
  let valueDisplay: React.ReactNode
  if (constraint.numeric_value !== undefined && constraint.numeric_value !== null) {
    const v = parseFloat(String(constraint.numeric_value))
    if (constraint.type === 'budget') {
      valueDisplay = <span style={{ color: '#C77DFF', fontWeight: 600 }}>${v.toFixed(2)}</span>
    } else if (constraint.type === 'functional') {
      valueDisplay = <span style={{ color: '#10B981', fontWeight: 600 }}>{v} 人份</span>
    } else {
      valueDisplay = <span style={{ fontWeight: 600 }}>{v}</span>
    }
  } else {
    valueDisplay = <span style={{ color: '#9CA3AF' }}>{constraint.content || '-'}</span>
  }

  // Type badge colors
  const typeColorMap: Record<string, { bg: string, text: string, icon: string }> = {
    budget: { bg: '#1a0f1a', text: '#C77DFF', icon: '💰' },
    functional: { bg: '#0a1a0a', text: '#10B981', icon: '👥' },
    taboo: { bg: '#1a0f0f', text: '#F87171', icon: '🚫' },
    preference: { bg: '#1a1a0a', text: '#F59E0B', icon: '⭐' },
  }
  const theme = typeColorMap[constraint.type] || { bg: '#1f2937', text: '#9CA3AF', icon: '📋' }

  return (
    <div style={{ 
      background: '#1e293b', 
      borderRadius: 8, 
      padding: 10,
      borderLeft: `3px solid ${theme.text}`,
      position: 'relative'
    }}>
      {/* Source indicator */}
      <div style={{ 
        position: 'absolute', 
        top: 6, 
        right: 32, 
        fontSize: 9, 
        padding: '2px 6px', 
        borderRadius: 99,
        background: isUserSource ? '#064e3b' : isLLMSource ? '#1e1b4b' : '#374151',
        color: isUserSource ? '#10B981' : isLLMSource ? '#C77DFF' : '#9CA3AF',
        fontWeight: 600
      }}>
        {isUserSource ? '用户' : isLLMSource ? '系统' : '自动'}
      </div>

      {/* 显式删除入口（自然语言匹配失败时的手动兜底，仍走 DELETE 端点） */}
      <button
        type="button"
        onClick={onDelete}
        disabled={deleting || !constraint.id}
        title="删除此约束"
        aria-label={`删除约束 ${constraint.content || constraint.type}`}
        style={{
          position: 'absolute', top: 4, right: 7,
          width: 18, height: 18, lineHeight: '14px', padding: 0,
          borderRadius: 4, border: '1px solid #4B5563', background: '#111827',
          color: deleting ? '#6B7280' : '#F87171', fontSize: 13, fontWeight: 700,
          cursor: (deleting || !constraint.id) ? 'default' : 'pointer',
        }}
      >
        {deleting ? '…' : '×'}
      </button>

      {/* Type + Value */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <span style={{ fontSize: 14 }}>{theme.icon}</span>
        <span style={{ fontSize: 12, fontWeight: 600, color: theme.text, textTransform: 'capitalize' }}>
          {constraint.type}
        </span>
        {constraint.hard && (
          <span style={{
            fontSize: 9,
            padding: '1px 5px',
            borderRadius: 4,
            background: '#450a0a',
            color: '#FCA5A5',
            fontWeight: 700,
          }}>
            硬约束
          </span>
        )}
      </div>

      {/* Value */}
      <div style={{ fontSize: 14, color: '#E5E7EB', marginBottom: 2 }}>
        {valueDisplay}
      </div>

      {/* Content hint if applicable */}
      {constraint.content && (
        <div style={{ fontSize: 10, color: '#6B7280', marginTop: 4, fontStyle: 'italic' }}>
          "{constraint.content}"
        </div>
      )}
    </div>
  )
}

// Global CSS for pulse animation
const styles = document.createElement('style')
styles.textContent = `
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
`
if (typeof document !== 'undefined' && !document.getElementById('constraints-dashboard-styles')) {
  styles.id = 'constraints-dashboard-styles'
  document.head.appendChild(styles)
}