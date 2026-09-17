/**
 * Decides whether the app renders behind a sign-in screen.
 *
 * Asks `GET /health` (the one route the server leaves open) whether auth is
 * required. When it is, the pool published there configures the auth core,
 * the API layer is handed a token provider, and `children` render only once
 * a session exists — `LoginPage` otherwise. When it is not (every local
 * `make dev`, the E2E suite), or the health check fails outright, `children`
 * render exactly as they did before auth existed: a local-first tool must
 * not lock itself behind a server that is down.
 *
 * A `401` from the API while signed in (a revoked user, a token the server
 * no longer accepts) drops the session and lands back here with a notice.
 */

import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import { setAuthTokenProvider } from '../api/http'
import { configureAuth, getFreshIdToken, isSignedIn, signOut } from './core'
import LoginPage from './LoginPage'
import { AuthProvider, useAuth } from './react'

type GateState = { kind: 'loading' } | { kind: 'open' } | { kind: 'required' }

export const SESSION_EXPIRED_NOTICE = 'Your session has ended — please sign in again.'

export interface AuthGateProps {
  children: ReactNode
  /** Rendered while the health check is in flight. */
  fallback?: ReactNode
}

export default function AuthGate({ children, fallback = null }: AuthGateProps) {
  const [state, setState] = useState<GateState>({ kind: 'loading' })
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .health()
      .then(health => {
        if (cancelled) return
        const auth = health.auth
        if (auth.required) {
          configureAuth({ region: auth.region, clientId: auth.client_id })
          setAuthTokenProvider({
            getToken: getFreshIdToken,
            onUnauthorized: () => {
              if (!isSignedIn()) return
              setNotice(SESSION_EXPIRED_NOTICE)
              void signOut()
            }
          })
          setState({ kind: 'required' })
        } else {
          setState({ kind: 'open' })
        }
      })
      .catch(() => {
        if (!cancelled) setState({ kind: 'open' })
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (state.kind === 'loading') return <>{fallback}</>
  if (state.kind === 'open') return <>{children}</>
  return (
    <AuthProvider>
      <Gate notice={notice} clearNotice={() => setNotice(null)}>
        {children}
      </Gate>
    </AuthProvider>
  )
}

function Gate({
  children,
  notice,
  clearNotice
}: {
  children: ReactNode
  notice: string | null
  clearNotice: () => void
}) {
  const { signedIn } = useAuth()
  useEffect(() => {
    if (signedIn && notice) clearNotice()
  }, [signedIn, notice, clearNotice])
  if (!signedIn) return <LoginPage notice={notice} />
  return <>{children}</>
}
