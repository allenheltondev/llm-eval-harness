/**
 * Transient notifications ("Run deleted", "Copied"), shown with the design
 * system's toasts.
 *
 * `useNotify` is the package's `useToast` with one difference: outside a
 * `<ToastProvider>` it returns a no-op instead of throwing. The app always
 * has one (`main.tsx`); a component test that renders a page on its own
 * does not need one unless it asserts on the toast, in which case it wraps
 * the page in `ToastProvider` itself.
 */

import { useToast, type ToastOptions } from '@readysetcloud/ui'
import type { ReactNode } from 'react'

export type Notify = (message: ReactNode, options?: ToastOptions) => void

const noop: Notify = () => undefined

export function useNotify(): Notify {
  try {
    // useToast is a context read that throws when the provider is missing;
    // the hook is still called unconditionally, so this is hook-safe.
    return useToast().toast
  } catch {
    return noop
  }
}
