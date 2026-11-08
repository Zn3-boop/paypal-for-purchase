import { useState } from 'react'
import type { FormEvent } from 'react'

type Props = {
  onSubmit: (text: string) => Promise<void>
  busy: boolean
  /** 最近一次提交的需求 —— 用于在方案面板顶部显示「正在为『xxx』检索」 */
  lastQuery: string | null
  lastMode: 'single' | 'group' | null
}

/**
 * 全局 Agent 搜索框（吸顶，拼多多式「上搜索下推荐」）：
 * 单人购买与团购共用这一个输入框。
 *
 * 为什么只有一个框（回答「为什么不能共用一个思想」）：
 *   - 提交 → /plans/intent（统一入口）→ 解析检索关键词 + 约束 + 模式
 *   - 然后整个页面就是：推荐方案 → 就地支付，全程不需要切换面板。
 * 吸顶不挡内容，也不像固定底部那样挤压移动端键盘。
 */
export function AgentComposer({ onSubmit, busy, lastQuery, lastMode }: Props) {
  const [text, setText] = useState('')
  const [localBusy, setLocalBusy] = useState(false)

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const value = text.trim()
    if (!value || busy || localBusy) return
    setLocalBusy(true)
    try {
      await onSubmit(value)
      setText('')
    } finally {
      setLocalBusy(false)
    }
  }

  const isBusy = busy || localBusy

  return (
    <div style={{
      position: 'sticky',
      top: 8,
      zIndex: 40,
      borderRadius: 12,
      padding: '10px 10px',
      marginBottom: 12,
      background: 'rgba(11,18,32,0.96)',
      border: `1px solid ${lastQuery && !isBusy ? '#3B82F6' : '#374151'}`,
      boxShadow: '0 6px 22px rgba(0,0,0,0.5)',
    }}>
      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <div style={{ flexShrink: 0, fontSize: 18 }}>{lastMode === 'group' ? '👥' : lastMode === 'single' ? '🧍' : '🤖'}</div>
        <input
          value={text}
          onChange={e => setText(e.target.value)}
          placeholder="一个框同时服务「单人购买」与「团购拼单」：帮我找 5 人份的环保桌游奖品，预算 300 美元；或：我要一张 R&B 黑胶唱片"
          aria-label="统一采购搜索框（单人/团购共用）"
          disabled={isBusy}
          style={{
            flex: 1, minWidth: 0, padding: '9px 10px', borderRadius: 8,
            border: 'none', outline: 'none', background: '#0b1220', color: '#E5E7EB', fontSize: 13,
          }}
        />
        {lastQuery && (
          <span title={`最近检索关键词：${lastQuery}`} style={{ flexShrink: 0, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 10, color: '#93C5FD' }}>
            🔎 {lastQuery}
          </span>
        )}
        <button
          type="submit"
          disabled={isBusy || !text.trim()}
          style={{
            flexShrink: 0, padding: '9px 16px', borderRadius: 8, border: 'none',
            background: isBusy ? '#1E3A5F' : '#3B82F6', color: '#fff', fontWeight: 700, fontSize: 13,
            cursor: (isBusy || !text.trim()) ? 'not-allowed' : 'pointer', opacity: (isBusy || !text.trim()) ? 0.55 : 1,
          }}
        >
          {isBusy ? '分析中…' : '去比价方案'}
        </button>
      </form>
    </div>
  )
}