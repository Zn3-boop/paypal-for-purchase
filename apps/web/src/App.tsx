import { useState } from 'react'
import { PaymentTab } from './components/tabs/PaymentTab'
import { Header } from './components/Header'
import { Footer } from './components/Footer'
import { ToastProvider } from './components/ToastProvider'
import { usePaymentAgent } from './hooks/usePaymentAgent'
import { ErrorBoundary } from './components/ErrorBoundary'

/** Single-purpose Agentic Commerce UI: product discovery, approval, and PayPal checkout. */
function AppShell() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const payment = usePaymentAgent({ setLoading, setError })

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', padding: '24px 16px 48px', minHeight: '100vh' }}>
      <ErrorBoundary>
        <Header />
        <PaymentTab
          chatMsg={payment.chatMsg}
          setChatMsg={payment.setChatMsg}
          history={payment.history}
          setHistory={payment.setHistory}
          endRef={payment.endRef}
          doChat={() => void payment.doChat()}
          decide={payment.decide}
          pending={payment.pending}
          loading={loading}
          onClear={payment.clear}
          goals={payment.goals}
          goalText={payment.goalText}
          setGoalText={payment.setGoalText}
          setGoal={() => void payment.setGoal()}
          goalSaving={payment.goalSaving}
        />
        {error && <div role="alert" style={{ color: '#FCA5A5', marginTop: 12, fontSize: 12 }}>{error}</div>}
      </ErrorBoundary>
      <Footer />
    </div>
  )
}

export default function App() {
  return <ToastProvider><AppShell /></ToastProvider>
}
