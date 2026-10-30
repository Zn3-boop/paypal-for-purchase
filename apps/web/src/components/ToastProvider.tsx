import { useCallback, useRef, useState, type ReactNode } from 'react'
import { ToastContainer } from './ToastContainer'
import { ToastContext, type Toast, type ToastFn } from '../hooks/useToast'

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const idRef = useRef(0)
  const dismiss = useCallback((id: number) => setToasts(t => t.filter(x => x.id !== id)), [])
  const push = useCallback<ToastFn>((message, type = 'info') => {
    const id = ++idRef.current
    setToasts(t => [...t, { id, message, type }])
    setTimeout(() => dismiss(id), 3500)
  }, [dismiss])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  )
}