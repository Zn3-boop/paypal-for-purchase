================================================================================
G3 手动测试指南 - PayPal Sandbox 完整批准 + Capture 流程
================================================================================

前提条件：
1. 后端服务运行在 http://localhost:8000
2. 环境变量已配置：
   - PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_SECRET
   - PAYPAL_RETURN_URL = http://localhost:8000/api/v1/paypal/callback
   - PAYPAL_CANCEL_URL = http://localhost:8000/api/v1/paypal/cancel

3. PayPal Developer 后台预建 Sandbox Buyer 账号：
   - 登录 https://developer.paypal.com
   - Accounts → Sandbox Accounts → Buyer 类型
   - 记下 email/password（例如：buyer@example.com / password123）

--------------------------------------------------------------------------------
步骤 1: 创建订单 (Create Order)
--------------------------------------------------------------------------------
方法 A - API 直接调用:
curl -X POST http://localhost:8000/api/v1/paypal/create_order \
  -H "Content-Type: application/json" \
  -d "{\"amount\": 600, \"session_id\": \"demo-recording\", \"user_email\": \"demo@example.com\", \"item_name\": \"ECV Group Purchase Demo\"}"

预期返回:
{
  "order_id": "XXXXXXXXXX",
  "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=XXXXXXXXXX",
  "status": "CREATED"
}

方法 B - 打开浏览器直接访问 approval_url

--------------------------------------------------------------------------------
步骤 2: PayPal 沙盒登录 + Approve (手动操作)
--------------------------------------------------------------------------------
1. 在浏览器打开 approval_url
2. 登录 Sandbox Buyer 账号：
   Email: <从 PayPal Developer 后台复制>
   Password: <从 PayPal Developer 后台复制>
3. 看到订单详情（金额 $600.00）
4. 点击 "Approve" 按钮

【镜头捕捉提示】录屏时要拍到：
- PayPal 登录页面（证明是真实 Sandbox）
- Approve 点击瞬间

--------------------------------------------------------------------------------
步骤 3: 回跳 + Capture (自动处理)
--------------------------------------------------------------------------------
PayPal 批准后会自动重定向到:
  http://localhost:8000/api/v1/paypal/callback?token=ORDER_TOKEN&PayerID=PAYER_ID

后端会自动执行:
1. 捕获订单：gateway.capture_order(order_id)
2. 验证状态：should be "COMPLETED"
3. 返回成功响应并 redirect 到前端 /payment/success

查看后端日志确认:
  [INFO] Order Captured: ORDER_ID status=COMPLETED
  [INFO] PayPal payment completed: order_id=XXX, amount=600.00, session_id=demo-recording

--------------------------------------------------------------------------------
步骤 4: 验证 COMPLETED 凭据
--------------------------------------------------------------------------------
成功响应应该包含:
{
  "success": true,
  "order_id": "XXXXXXXXXX",
  "status": "COMPLETED",
  "amount": 600.0,
  "seller_protection": "ELIGIBLE",
  "redirect_url": "http://localhost:5173/payment/success?...",
  "message": "支付成功，订单已创建"
}

关键证据：
- order_id 不为空
- status = "COMPLETED"
- amount 与实际一致

--------------------------------------------------------------------------------
步骤 5: 取消分支测试 (手动操作 Cancel)
--------------------------------------------------------------------------------
1. 重新创建订单，拿到新的 approval_url
2. 打开浏览器访问 approval_url
3. 登录后点击 "Cancel"（不要 Approve）
4. PayPal 重定向到 /cancel
5. 后端返回 CANCELLED 状态
6. 前端 redirect 回复核面板 /payment/review?status=cancelled

预期响应:
{
  "success": false,
  "status": "CANCELLED",
  "message": "用户取消支付",
  "redirect_url": "http://localhost:5173/payment/review?..."
}

--------------------------------------------------------------------------------
验收标准（全部打勾才算通过）
--------------------------------------------------------------------------------
[ ] 成功链路连续跑通 2 次：
    ✓ Order Created → Approve → Callback → COMPLETED → 真实 order_id
    
[ ] 取消分支跑通 1 次：
    ✓ Order Created → Cancel → CANCELLED → 返回复核面板

[ ] 日志验证：
    ✓ 能看到 capture 成功的日志（status=COMPLETED）
    ✓ seller_protection 字段正常

[ ] 金额验证：
    ✓ 使用真实金额（如 $600），而不是 $1

--------------------------------------------------------------------------------
常见问题排查
--------------------------------------------------------------------------------
1. Error: "INVALID_REQUEST"
   → 检查 PAYPAL_SANDBOX_CLIENT_ID/SECRET 是否正确
   
2. Error: "TOKEN_EXPIRED"
   → approval_url 超过 3 小时过期，重新 create_order
   
3. Capture 后状态仍是 "CREATED" 不是 "COMPLETED"
   → 需要用户实际批准，否则 capture 会失败
   
4. 回跳 URL 404
   → 确认后端路由已挂载 /api/v1/paypal/callback

--------------------------------------------------------------------------------
测试完成标志
--------------------------------------------------------------------------------
拿到以下凭据后可进入下一阶段：
1. 2 次 COMPLETED order_id（抄到种子脚本）
2. 1 次 CANCELLED 成功回退
3. 后端日志截图（证明 capture 真的走了）

================================================================================
