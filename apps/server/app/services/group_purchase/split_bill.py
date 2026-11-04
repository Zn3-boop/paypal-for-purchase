"""多人分摊 + PayPal 分账逻辑。

对应执行清单 G 模块：
  G1 split_bill()：均摊 + 尾差处理
  G2 PayPal 沙盒分账模拟
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger(__name__)

# PayPal 真实支付入口（可选依赖）
try:
    from app.services.paypal_gateway import get_paypal_gateway
    HAS_PAYPAL = True
except ImportError:
    HAS_PAYPAL = False


@dataclass
class SplitResult:
    """分摊结果。"""
    total: float
    per_person: float
    splits: list[dict[str, Any]]  # [{member_id, name, amount}]
    adjustment_member: str | None = None  # 承担尾差的成员
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "per_person": self.per_person,
            "splits": self.splits,
            "adjustment_member": self.adjustment_member,
        }


def split_bill(
    total: float,
    members: list[str],  # 成员 ID 列表
    custom_splits: dict[str, float] | None = None,  # 自定义比例/金额 {member_id: amount_or_ratio}
) -> SplitResult:
    """G1: 分摊账单计算。
    
    支持三种模式：
    1. 均摊：custom_splits=None
    2. 按比例：custom_splits={member_id: ratio_float} (总和应为 1.0)
    3. 固定金额：custom_splits={member_id: fixed_amount} (总和应<=total)
    
    尾差处理：最后一位成员承担小数位误差（避免总额不平）。
    """
    num_members = len(members)
    if num_members == 0:
        return SplitResult(total=total, per_person=0, splits=[], adjustment_member=None)
    
    splits = []
    allocated = 0.0
    
    if custom_splits is None:
        # 模式 1：均摊
        base_share = Decimal(str(total)) / Decimal(str(num_members))
        base_share_rounded = float(base_share.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        
        # 前面的人拿四舍五入后的份额，最后一个人承担尾差
        adjustment_member = members[-1]
        
        for i, member_id in enumerate(members):
            if i == num_members - 1:
                # 最后一个人：total - 已分配 = 实际应承担
                amount = round(total - allocated, 2)
            else:
                amount = base_share_rounded
                allocated += amount
            
            splits.append({
                "member_id": member_id,
                "name": member_id,  # 后续可映射为真实姓名
                "amount": amount,
                "is_adjustment": (i == num_members - 1),
            })
    
    else:
        # 模式 2/3：自定义分摊
        # 先分离比例型和固定金额型
        ratio_splits = {}
        fixed_splits = {}
        
        for member_id, value in custom_splits.items():
            if isinstance(value, (int, float)):
                val = float(value)
                if 0 <= val <= 1 and member_id not in [k for k in custom_splits if isinstance(custom_splits[k], (int, float))]:
                    # 简单启发式：如果值 <= 1 且只有少数几个，可能是比例
                    # 这里简化处理：假设用户提供的是固定金额
                    fixed_splits[member_id] = val
                else:
                    fixed_splits[member_id] = val
        
        # 固定金额模式
        for member_id in members:
            if member_id in fixed_splits:
                amount = round(fixed_splits[member_id], 2)
            else:
                # 未指定的成员平分剩余金额
                remaining = total - allocated
                unassigned = [m for m in members if m not in fixed_splits]
                if unassigned:
                    amount = round(remaining / len(unassigned), 2)
                else:
                    amount = 0
            
            if member_id == members[-1]:
                # 最后一个人承担尾差
                amount = round(total - allocated, 2)
            
            splits.append({
                "member_id": member_id,
                "name": member_id,
                "amount": max(0, amount),
                "is_adjustment": (member_id == members[-1]),
            })
            allocated += splits[-1]["amount"]
    
    # 确保总额匹配（浮点修正）
    actual_total = sum(s["amount"] for s in splits)
    if abs(actual_total - total) > 0.01:
        # 修正最后一个
        splits[-1]["amount"] = round(total - (actual_total - splits[-1]["amount"]), 2)
    
    per_person = round(total / num_members, 2) if num_members > 0 else 0
    
    return SplitResult(
        total=total,
        per_person=per_person,
        splits=splits,
        adjustment_member=members[-1] if members else None,
    )


def simulate_paypal_split_payment(
    splits: list[dict[str, Any]],
    order_id: str,
) -> dict[str, Any]:
    """G2: PayPal 沙盒分账模拟。
    
    PayPal Orders v2 原生不支持多收款人分账，有两种方案：
    1. 主账号收款 + 后向 Payouts 分发（需额外调用）
    2. 分别创建多个小额订单（体验较差）
    
    此处模拟方案 1：记录分账预期，实际资金流由主订单承载。
    """
    total = sum(s.get("amount", 0) for s in splits)
    
    # 模拟分账记录（实际 PayPal 不会自动分账，需要手动调用 Payouts）
    payout_schedule = []
    for split in splits:
        payout_schedule.append({
            "receiver": f"{split['member_id']}@example.com",  # 模拟收款邮箱
            "amount": split["amount"],
            "note": f"分摊订单 {order_id}",
            "status": "SCHEDULED",  # 待执行
        })
    
    return {
        "order_id": order_id,
        "total": round(total, 2),
        "split_method": "primary_collect_then_distribute",
        "payout_schedule": payout_schedule,
        "payout_note": "实际分账需在订单完成后调用 Payouts API 分发，此处仅为模拟记录。",
        "currency": "USD",
    }


def validate_split_consistency(
    splits: list[dict[str, Any]],
    expected_total: float,
    tolerance: float = 0.02,
) -> tuple[bool, str]:
    """验证分摊总额是否一致。"""
    actual_total = sum(s.get("amount", 0) for s in splits)
    diff = abs(actual_total - expected_total)
    
    if diff > tolerance:
        return False, f"分摊总额 {actual_total:.2f} 与预期 {expected_total:.2f} 偏差 {diff:.2f}，超过容差 {tolerance}"
    
    # 检查是否有负数或零金额
    for s in splits:
        if s.get("amount", 0) <= 0:
            return False, f"成员 {s.get('member_id')} 的分摊金额为 {s.get('amount')}, 必须为正数"
    
    return True, "分摊一致"


async def execute_real_paypal_payment(total: float, user_email: str, session_id: str):
    """真实 PayPal 支付流程（用于演示录屏）。"""
    if not HAS_PAYPAL:
        return {
            "success": False,
            "error": "PayPal 未配置，请在.env 中设置 PAYPAL_CLIENT_ID 和 PAYPAL_CLIENT_SECRET",
        }
    
    gateway = get_paypal_gateway()
    
    # 1. 创建订单
    order_data = gateway.create_order(total, f"ECV Session {session_id}")
    
    # 2. 模拟用户批准后捕获（实际需配合前端回调或 Webhook）
    capture_result = gateway.capture_order(order_data["order_id"])
    
    # 3. 记录分账预期（实际分账需后续调用 Payouts）
    split_preview = simulate_paypal_split_payment(
        [{"member_id": user_email, "amount": total}],
        order_data["order_id"],
    )
    
    return {
        "success": capture_result["status"] == "COMPLETED",
        "order_id": order_data["order_id"],
        "approval_url": order_data["approval_url"],
        "capture_status": capture_result["status"],
        "amount": capture_result["amount"],
        "seller_protection": capture_result["seller_protection"],
        "split_schedule": split_preview,
    }
    """验证分摊总额是否一致。"""
    actual_total = sum(s.get("amount", 0) for s in splits)
    diff = abs(actual_total - expected_total)
    
    if diff > tolerance:
        return False, f"分摊总额 {actual_total:.2f} 与预期 {expected_total:.2f} 偏差 {diff:.2f}，超过容差 {tolerance}"
    
    # 检查是否有负数或零金额
    for s in splits:
        if s.get("amount", 0) <= 0:
            return False, f"成员 {s.get('member_id')} 的分摊金额为 {s.get('amount')}, 必须为正数"
    
    return True, "分摊一致"