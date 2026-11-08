/** 事件元数据：图标、颜色、中文标签。五个核心事件高亮（审计时间轴与订单详情共用）。 */
export const EVENT_META: Record<string, { icon: string; color: string; label: string; core: boolean }> = {
  constraint_changed: { icon: '📋', color: '#60A5FA', label: '约束变更', core: true },
  plan_generated: { icon: '🗺️', color: '#34D399', label: '方案生成', core: true },
  cart_modified: { icon: '🛒', color: '#FBBF24', label: '购物车修改', core: false },
  review_confirmed: { icon: '✅', color: '#F97316', label: '人工确认', core: true },
  payment_completed: { icon: '💳', color: '#A78BFA', label: '支付完成', core: true },
  conflict_detected: { icon: '⚔️', color: '#F87171', label: '冲突检测', core: false },
  budget_violated: { icon: '🚨', color: '#EF4444', label: '预算超限', core: false },
  fallback_triggered: { icon: '🔁', color: '#9CA3AF', label: '降级触发', core: false },
  order_status_changed: { icon: '🔄', color: '#60A5FA', label: '订单状态变更', core: false },
  // 统一入口 + 支付留痕（payment_started / payment_failed 与订单档案里的同名单号互相对应）
  intent_submitted: { icon: '⌨️', color: '#38BDF8', label: '需求提交', core: false },
  payment_started: { icon: '🚀', color: '#22D3EE', label: '支付发起', core: false },
  payment_failed: { icon: '⚠️', color: '#FB7185', label: '支付失败', core: false },
  payment_cancelled: { icon: '🚫', color: '#F59E0B', label: '支付取消', core: false },
}