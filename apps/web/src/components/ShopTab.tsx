import { PlansTab } from './tabs/PlansTab'

/**
 * 「搜索即推荐」单页：只留 方案对比 一个区域。
 *
 * 为什么删掉下面原来的「就地支付 / 对话」：它让一次采购变成两次支付——
 * 先按方案下单，再让代理**自己重新选品**（于是买到与你无关的 Merch Tee，
 * 还弹出「待审批订单」）。现在唯一路径是：
 *   选方案 → 二次确认弹窗（金额 / 改单 / 守卫）→ PayPal。
 */
type Props = {
  sessionId: string
  onSelectPlan: (planId: 'A' | 'B') => Promise<void>
  selectedPlanId: 'A' | 'B' | null
  refreshKey?: number
  intentQuery?: string | null
  intentMode?: 'single' | 'group' | null
}

export function ShopTab({
  sessionId,
  onSelectPlan,
  selectedPlanId,
  refreshKey,
  intentQuery,
  intentMode,
}: Props) {
  return (
    <PlansTab
      sessionId={sessionId}
      onSelectPlan={onSelectPlan}
      selectedPlanId={selectedPlanId}
      refreshKey={refreshKey}
      intentQuery={intentQuery}
      intentMode={intentMode}
    />
  )
}

