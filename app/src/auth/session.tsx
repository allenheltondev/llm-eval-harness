/**
 * What the rest of the app knows about the signed-in user.
 *
 * Sign-in itself is the Ready, Set, Cloud auth package's
 * (`@readysetcloud/ui/auth`), whose `useAuth()` only works inside its
 * provider. Nimbus renders without one whenever the server says sign-in is
 * not required (every local run, the E2E suite), so components read this
 * context instead: `AuthGate` fills it from the package's state when there is
 * a provider, and it answers "no auth" when there is not.
 */

import { createContext, useContext } from 'react'
import type { IdClaims } from '@readysetcloud/ui/auth'

export interface Session {
  /** Whether this deployment requires sign-in at all. */
  required: boolean
  /** True when there is a session. */
  signedIn: boolean
  /** Decoded ID-token claims (`{}` when signed out). */
  user: IdClaims
  /** Clear the session and best-effort revoke the refresh token. */
  signOut: () => Promise<void>
}

export const NO_AUTH: Session = {
  required: false,
  signedIn: false,
  user: {},
  signOut: async () => undefined
}

export const SessionContext = createContext<Session>(NO_AUTH)

export function useSession(): Session {
  return useContext(SessionContext)
}

/** The name to show for a user: their given and family name, else their email. */
export function displayName(user: IdClaims): string | undefined {
  const name = [user.given_name, user.family_name].filter(Boolean).join(' ')
  if (name) return name
  return typeof user.email === 'string' ? user.email : undefined
}
