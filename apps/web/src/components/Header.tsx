import { useState } from 'react'

function buildShareUrl() {
  const params = new URLSearchParams({
    query: 'noise cancelling headphones under 300',
    location: 'New York',
  })
  return `${window.location.origin}${window.location.pathname}?${params.toString()}`
}

/** Brand header for the AI Shopping / PayPal checkout flow. */
export function Header() {
  const [copied, setCopied] = useState(false)
  const handleShare = async () => {
    try {
      await navigator.clipboard.writeText(buildShareUrl())
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* best effort */ }
  }

  return (
    <header style={{ textAlign: 'center', marginBottom: 28 }}>
      <div style={{ fontSize: 11, color: '#F5A623', letterSpacing: 3, textTransform: 'uppercase', fontWeight: 600 }}>Channel3 × PayPal Agentic Commerce</div>
      <h1 style={{ fontSize: 32, fontWeight: 800, margin: '8px 0 4px', background: 'linear-gradient(135deg, #FF6B35, #F5A623)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>AI Shopping</h1>
      <p style={{ fontSize: 15, color: '#9CA3AF', maxWidth: 600, margin: '0 auto', lineHeight: 1.6 }}>
        用自然语言描述商品，AI 搜索真实商品；你确认购物清单后，再通过 PayPal 完成支付。<br />
        <strong style={{ color: '#E5E7EB' }}>AI 建议，人做最终决定</strong>，每一步都有来源和授权依据。
      </p>
      <button onClick={handleShare} style={{ marginTop: 12, fontSize: 11, padding: '4px 10px', borderRadius: 8, background: '#1F2937', color: copied ? '#6EE7B7' : '#9CA3AF', border: '1px solid #374151', cursor: 'pointer', fontWeight: 600 }}>
        {copied ? '✓ Copied!' : '🔗 Share this demo'}
      </button>
    </header>
  )
}
