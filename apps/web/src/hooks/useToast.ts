import { createContext, useContext } from 'react'

export type ToastType = 'success' | 'error' | 'info'
export type Toast = { id: number; message: string; type: ToastType }
export type ToastFn = (message: string, type?: ToastType) => void

export const ToastContext = createContext<ToastFn>(() => {})

/** Call `toast('Saved', 'success')` from any component under <ToastProvider>. */
export function useToast(): ToastFn {
  return useContext(ToastContext)
}