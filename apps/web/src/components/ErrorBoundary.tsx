import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { sx } from './sx'

/**
 * Top-level crash shield. Any uncaught render/lifecycle error used to blank the
 * whole page ("黑屏需要刷新"); instead the boundary renders a recoverable card
 * with a one-click reload — the demo can always get back on the rails.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary] UI crashed:', error, info)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div style={{ ...sx.card, maxWidth: 520, margin: '10vh auto', textAlign: 'center', padding: 40 }}>
        <div style={{ fontSize: 36, marginBottom: 10 }}>⚠️</div>
        <h2 style={{ fontSize: 18, fontWeight: 700, color: '#E5E7EB', marginBottom: 8 }}>Something went wrong</h2>
        <p style={{ fontSize: 12, color: '#9CA3AF', marginBottom: 16, wordBreak: 'break-word' }}>
          {String(this.state.error?.message || this.state.error)}
        </p>
        <button
          onClick={() => { this.setState({ error: null }); window.location.reload() }}
          style={{ ...sx.btnPrimary, background: 'linear-gradient(135deg, #8B5CF6, #7C3AED)', cursor: 'pointer' }}
        >
          ↺ Reload demo
        </button>
      </div>
    )
  }
}