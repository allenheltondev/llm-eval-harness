/**
 * React bindings for the auth core (the rsc-core `AuthProvider`/`useAuth`
 * shape). Router-agnostic: there is no router here, `AuthGate` decides what
 * to render.
 *
 * `useAuth()` outside a provider does not throw — it answers "auth is not
 * required" so components like the app header can render a sign-out control
 * conditionally without caring whether the deployment has auth at all.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode
} from 'react'
import {
  claims,
  getFreshIdToken,
  installStorageListener,
  isSignedIn,
  onAuthChange,
  signOut as coreSignOut,
  type IdClaims
} from './core'

export interface AuthState {
  /** Whether this deployment requires sign-in at all. */
  required: boolean
  /** True when a session document exists. */
  signedIn: boolean
  /** Decoded ID-token claims (`{}` when signed out). */
  user: IdClaims
  /** A valid ID token for API calls, refreshing near expiry. */
  getToken: () => Promise<string | null>
  /** Clear the session and best-effort revoke the refresh token. */
  signOut: () => Promise<void>
}

const NO_AUTH: AuthState = {
  required: false,
  signedIn: false,
  user: {},
  getToken: async () => null,
  signOut: async () => undefined
}

const AuthContext = createContext<AuthState>(NO_AUTH)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState<boolean>(() => isSignedIn())
  const [user, setUser] = useState<IdClaims>(() => claims())

  useEffect(() => {
    installStorageListener()
    const sync = () => {
      setSignedIn(isSignedIn())
      setUser(claims())
    }
    sync()
    return onAuthChange(sync)
  }, [])

  const getToken = useCallback(() => getFreshIdToken(), [])
  const signOut = useCallback(() => coreSignOut(), [])

  const value = useMemo<AuthState>(
    () => ({ required: true, signedIn, user, getToken, signOut }),
    [signedIn, user, getToken, signOut]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  return useContext(AuthContext)
}
