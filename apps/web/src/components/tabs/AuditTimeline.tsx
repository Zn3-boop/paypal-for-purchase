import { useEffect, useState } from 'react'
import { fetchAuditLogs, type AuditLogEntry } from '../../api'
import { EVENT_META } from './auditEventMeta'
import { EventDataSummary } from './auditEventSummary'

type Props = {
  sessionId: string
}

/**
 * 一个「采购任务」= 一次统一入口提交（intent_submitted，带 data.query）。
 * 同一 session_id 承载工作台 / 任务 / 审计归档 / 订单归属四重身份，因此旧任务的
 * 审计会一直挂在当前视图里。不新建 task_id（架构级改动、波及 13 张事件表），
 * 前端就地按 intent_submitted 切组：最新任务在最上面、默认展开，历史任务折叠。
 */
type TaskGroup = {
  key: string
  query: string
  ts: string
  events: AuditLogEntry[]
}

function groupByTask(logs: AuditLogEntry[]): TaskGroup[] {
  const groups: TaskGroup[] = []
  let current: TaskGroup | null = null
  for (const log of logs) {
    if (log.event === 'intent_submitted') {
      current = {
        key: `task@${log.timestamp}`,
        query: String((log.data?.query as string) ?? '').trim(),
        ts: log.timestamp,
        events: [log],
      }
      groups.push(current)
    } else {
      if (!current) {
        // intent_submitted 之前的历史（跑旧流程留下的记录）：统一塞进一个"前言"组
        current = { key: 'task@pre', query: '', ts: log.timestamp, events: [] }
        groups.push(current)
      }
      current.events.push(log)
    }
  }
  return groups
}

const entryKey = (log: AuditLogEntry) => `${log.timestamp}-${log.event}-${log.round_num}`

export function AuditTimeline({ sessionId }: Props) {
  const [logs, setLogs] = useState<AuditLogEntry[]>([])
  // 单条事件展开态（key = timestamp-event-round）
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  // 任务组折叠覆盖：manualGroup[key] 由用户显式给出；缺省按「最新展开、历史折叠」
  const [manualGroup, setManualGroup] = useState<Record<string, boolean>>({})
  // 与「订单档案」去重：订单状态流转（order_status_changed 等）在订单档案里更完整，
  // 这里默认仍全量展示（可追溯），但可一键只看核心决策事件。
  const [coreOnly, setCoreOnly] = useState(false)

  useEffect(() => {
    if (!sessionId) return
    let alive = true
    const load = () => {
      fetchAuditLogs(sessionId)
        .then(res => {
          if (alive) setLogs(res.logs || [])
        })
        .catch(() => { /* silent — audit panel is non-critical */ })
    }
    load()
    const interval = setInterval(load, 4000)
    return () => { alive = false; clearInterval(interval) }
  }, [sessionId])

  const coreCount = logs.filter(l => EVENT_META[l.event]?.core).length
  const orderNoise = logs.filter(l => l.event === 'order_status_changed').length
  const visibleLogs = coreOnly ? logs.filter(l => EVENT_META[l.event]?.core) : logs
  const groups = groupByTask(visibleLogs)
  const renderedGroups = groups.slice().reverse() // 最新任务在顶

  const isGroupCollapsed = (key: string, renderedIdx: number) =>
    key in manualGroup ? manualGroup[key] : renderedIdx !== 0

  return (
    <div style={{ background: '#0f172a', borderRadius: 12, padding: 16, border: '1px solid #374151' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 8, flexWrap: 'wrap' }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: '#E5E7EB' }}>📜 审计时间轴</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button
            onClick={() => { setCoreOnly(v => !v); setExpandedKey(null) }}
            title="隐藏订单状态流转（这些记录在 📦 订单档案 里更完整）"
            style={{
              fontSize: 10, padding: '3px 8px', borderRadius: 99, cursor: 'pointer',
              border: `1px solid ${coreOnly ? '#3B82F6' : '#4B5563'}`,
              background: coreOnly ? '#1E3A5F' : 'transparent',
              color: coreOnly ? '#93C5FD' : '#9CA3AF', fontWeight: 700,
            }}
          >
            {coreOnly ? '只看核心事件 ✓' : '只看核心事件'}
          </button>
          {logs.length > 0 && (
            <div style={{ fontSize: 11, color: '#9CA3AF' }}>
              {coreCount}/5 核心事件
            </div>
          )}
        </div>
      </div>

      {/* 分工说明：审计=「为什么这么决策」，订单档案=「订单现在到哪一步」 */}
      <div style={{ fontSize: 10, color: '#6B7280', marginBottom: 10, lineHeight: 1.5 }}>
        本面板记录决策链（需求提交 → 约束解析 → 检索 → 选方案 → 审批 → 支付），
        按「提交的需求」分组；{orderNoise > 0 ? `订单状态流转（${orderNoise} 条）` : '订单状态流转'}请到 📦 订单档案 查看（已支付订单不随会话过期）。
      </div>

{visibleLogs.length === 0 ? (
        <div style={{ fontSize: 12, color: '#6B7280', fontStyle: 'italic', textAlign: 'center', padding: 20 }}>
          {logs.length === 0
            ? '暂无审计记录 — 开始一次团购流程后，每个决策步骤都会在此留痕。'
            : '已过滤掉订单状态流转事件 — 取消「只看核心事件」可查看全量。'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 420, overflowY: 'auto' }}>
          {renderedGroups.map((group, gIdx) => {
            const collapsed = isGroupCollapsed(group.key, gIdx)
            const isCurrent = gIdx === 0
            return (
              <div key={group.key} style={{ border: '1px solid #1f2937', borderRadius: 10, padding: '8px 10px' }}>
                {/* 任务组头 */}
                <div
                  onClick={() => setManualGroup(m => ({ ...m, [group.key]: !collapsed }))}
                  style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', flexWrap: 'wrap' }}
                >
                  <span style={{ fontSize: 11, color: isCurrent ? '#FCD34D' : '#94A3B8', transition: 'transform 0.15s', display: 'inline-block', transform: collapsed ? 'rotate(-90deg)' : 'none' }}>▼</span>
                  <span style={{ fontSize: 12, fontWeight: 700, color: '#E5E7EB' }}>
                    {group.query ? `🔎 ${group.query}` : '📌 此前操作（尚未提交需求）'}
                  </span>
                  {isCurrent && (
                    <span style={{ fontSize: 9, background: '#78350F', color: '#FCD34D', padding: '1px 6px', borderRadius: 99, fontWeight: 700 }}>
                      当前任务
                    </span>
                  )}
                  <span style={{ fontSize: 10, color: '#6B7280' }}>
                    {(group.ts || '').slice(11, 19)} UTC · {group.events.length} 条
                    {group.events.some(e => EVENT_META[e.event]?.core) && ` · ${group.events.filter(e => EVENT_META[e.event]?.core).length} 核心`}
                  </span>
                </div>

                {!collapsed && (
                  <div style={{ marginTop: 4 }}>
                    {group.events.map((log, idx) => {
                      const meta = EVENT_META[log.event] || { icon: '·', color: '#6B7280', label: log.event, core: false }
                      const isLast = idx === group.events.length - 1
                      const isOpen = expandedKey === entryKey(log)
                      return (
                        <div key={entryKey(log)} style={{ display: 'flex', gap: 10 }}>
                          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 20 }}>
                            <div style={{
                              width: meta.core ? 12 : 8,
                              height: meta.core ? 12 : 8,
                              borderRadius: '50%',
                              background: meta.color,
                              marginTop: 4,
                              flexShrink: 0,
                              boxShadow: meta.core ? `0 0 6px ${meta.color}66` : 'none',
                            }} />
                            {!isLast && <div style={{ width: 2, flex: 1, background: '#374151' }} />}
                          </div>
                          <div
                            onClick={() => setExpandedKey(isOpen ? null : entryKey(log))}
                            style={{ flex: 1, paddingBottom: 10, cursor: 'pointer', minWidth: 0 }}
                          >
                            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
                              <span style={{ fontSize: 12, fontWeight: meta.core ? 700 : 500, color: meta.color }}>
                                {meta.icon} {meta.label}
                              </span>
                              <span style={{ fontSize: 10, color: '#6B7280' }}>
                                {(log.timestamp || '').slice(11, 19)} UTC
                              </span>
                              {log.actor && (
                                <span style={{ fontSize: 9, color: '#9CA3AF', background: '#1f2937', padding: '1px 6px', borderRadius: 99 }}>
                                  {log.actor === 'user' ? '👤 用户' : log.actor === 'system' ? '⚙️ 系统' : log.actor}
                                </span>
                              )}
                            </div>

                            {!isOpen && <EventDataSummary log={log} />}

                            {isOpen && (
                              <div style={{
                                marginTop: 6,
                                background: '#111827',
                                border: '1px solid #374151',
                                borderRadius: 8,
                                padding: 8,
                                fontFamily: 'ui-monospace, monospace',
                                fontSize: 10,
                                color: '#9CA3AF',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all',
                              }}>
                                {JSON.stringify({ ...log, constraint_snapshot: undefined, plan_snapshot: undefined }, null, 1)
                                  .replace(/[{}\"]/g, '')
                                  .slice(0, 600)}
                                {log.plan_snapshot && (
                                  <div style={{ marginTop: 4, color: '#FBBF24' }}>
                                    商品快照：{log.plan_snapshot.length} 件
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}