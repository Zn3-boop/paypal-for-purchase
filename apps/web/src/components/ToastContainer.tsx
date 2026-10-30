import type { Toast } from '../hooks/useToast'

const COLORS: Record<Toast['type'], string> = {
  success: '#065F46', error: '#7F1D1D', info: '#1F2937',
}
const ICONS: Record<Toast['type'], string> = {
  success: '✓', error: '✕', info: '💡',
}

// Pure presentational toast stack. Fixed bottom-right; auto-dismiss handled by the provider.
export function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  if (toasts.length === 0) return null
  return (
    <div style={{ position: 'fixed', bottom: 20, right: 20, display: 'flex', flexDirection: 'column', gap: 8, zIndex: 9999, maxWidth: 'min(360px, 90vw)' }}>
      {toasts.map(t => (
        <div key={t.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px', borderRadius: 10, background: COLORS[t.type], border: '1px solid #374151', color: '#fff', fontSize: 13, lineHeight: 1.4, boxShadow: '0 6px 20px rgba(0,0,0,0.45)' }}>
          <span style={{ fontWeight: 800 }}>{ICONS[t.type]}</span>
          <span style={{ flex: 1 }}>{t.message}</span>
          <button onClick={() => onDismiss(t.id)} title="Dismiss" style={{ background: 'none', border: 'none', color: '#D1D5DB', cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: 0 }}>×</button>
        </div>
      ))}
    </div>
  )
}