/**
 * Decides whether the app renders behind a sign-in screen.
 *
 * Asks `GET /health` (the one route the server leaves open) whether auth is
 * required. When it is, the pool published there configures the Ready, Set,
 * Cloud auth package, the API layer is handed its token provider, and
 * `children` render only once a session exists — the package's sign-in flows
 * otherwise. When it is not (every local `make dev`, the E2E suite), or the
 * health check fails outright, `children` render exactly as they did before
 * auth existed: a local-first tool must not lock itself behind a server that
 * is down.
 *
 * Two answers from the API change what is shown while signed in:
 *
 * - `401` (a revoked user, a token the server no longer accepts) drops the
 *   session and lands back on sign-in with a notice;
 * - `403` means signed in but not granted this stack — with a shared pool,
 *   having an account is not access; the server also requires a group. That
 *   shows a no-access screen naming the group, with a way to sign out.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Alert, Button, Card, CardBody, LoadingPage } from '@readysetcloud/ui'
import {
  AuthProvider,
  ForgotPasswordForm,
  LoginForm,
  SignUpForm,
  configureAuth,
  getFreshIdToken,
  isSignedIn,
  signOut,
  useAuth
} from '@readysetcloud/ui/auth'
import { api } from '../api/client'
import { setAuthTokenProvider } from '../api/http'
import { SessionContext, displayName, type Session } from './session'

type GateState =
  { kind: 'loading' } | { kind: 'open' } | { kind: 'required'; requiredGroup: string | null }

export const SESSION_EXPIRED_NOTICE = 'Your session has ended — please sign in again.'

export interface AuthGateProps {
  children: ReactNode
  /** Rendered while the health check is in flight. */
  fallback?: ReactNode
}

export default function AuthGate({
  children,
  fallback = <LoadingPage text="Connecting…" />
}: AuthGateProps) {
  const [state, setState] = useState<GateState>({ kind: 'loading' })
  const [notice, setNotice] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

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
            },
            onForbidden: () => setForbidden(true)
          })
          setState({ kind: 'required', requiredGroup: auth.required_group ?? null })
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
      <Gate
        notice={notice}
        clearNotice={() => setNotice(null)}
        forbidden={forbidden}
        clearForbidden={() => setForbidden(false)}
        requiredGroup={state.requiredGroup}
      >
        {children}
      </Gate>
    </AuthProvider>
  )
}

function Gate({
  children,
  notice,
  clearNotice,
  forbidden,
  clearForbidden,
  requiredGroup
}: {
  children: ReactNode
  notice: string | null
  clearNotice: () => void
  forbidden: boolean
  clearForbidden: () => void
  requiredGroup: string | null
}) {
  const { signedIn, user, signOut: packageSignOut } = useAuth()

  useEffect(() => {
    if (signedIn && notice) clearNotice()
    // A different account (or none) starts over: the refusal was this one's.
    if (!signedIn && forbidden) clearForbidden()
  }, [signedIn, notice, clearNotice, forbidden, clearForbidden])

  const session = useMemo<Session>(
    () => ({ required: true, signedIn, user, signOut: packageSignOut }),
    [signedIn, user, packageSignOut]
  )

  // Accounts are open to anyone only on a pool that gates by group (the
  // shared RSC pool); a stack's own pool is invitation-only, where a sign-up
  // form would only lead to Cognito refusing it.
  if (!signedIn) return <SignIn notice={notice} allowSignUp={requiredGroup !== null} />
  if (forbidden) {
    return (
      <NoAccess
        who={displayName(user)}
        requiredGroup={requiredGroup}
        onSignOut={() => void packageSignOut()}
      />
    )
  }
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
}

/** The Nimbus lockup above each sign-in card: the brand mark and the app name. */
function Logo() {
  return (
    <span className="app-nav-brand" aria-hidden="true">
      <span className="app-nav-brand-mark" />
      <span className="app-nav-brand-name">Nimbus</span>
    </span>
  )
}

type Flow =
  | { name: 'sign-in' }
  | { name: 'sign-up'; email?: string; password?: string }
  | { name: 'forgot'; email?: string; startAtReset?: boolean }

/**
 * The package's sign-in flows, switched with local state: there is no router,
 * and the address bar belongs to the app's own hash routes.
 */
function SignIn({ notice, allowSignUp }: { notice: string | null; allowSignUp: boolean }) {
  const [flow, setFlow] = useState<Flow>({ name: 'sign-in' })
  const toSignIn = () => setFlow({ name: 'sign-in' })
  const linkButton = (label: string, onClick: () => void) => (
    <button type="button" className="auth-link" onClick={onClick}>
      {label}
    </button>
  )

  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center gap-4 px-4 py-10">
      {notice && (
        <div className="w-full max-w-md">
          <Alert variant="info">{notice}</Alert>
        </div>
      )}
      {flow.name === 'sign-in' && (
        <LoginForm
          logo={<Logo />}
          onSuccess={() => undefined}
          onNeedsConfirmation={(email, password) => setFlow({ name: 'sign-up', email, password })}
          onPasswordResetRequired={email => setFlow({ name: 'forgot', email, startAtReset: true })}
          forgotPasswordLink={linkButton('Forgot password?', () => setFlow({ name: 'forgot' }))}
          signUpPrompt={
            allowSignUp ? (
              <>
                New to Ready, Set, Cloud?{' '}
                {linkButton('Create an account', () => setFlow({ name: 'sign-up' }))}
              </>
            ) : undefined
          }
        />
      )}
      {flow.name === 'sign-up' && (
        <SignUpForm
          logo={<Logo />}
          onSuccess={toSignIn}
          initialConfirmEmail={flow.email}
          initialConfirmPassword={flow.password}
          signInPrompt={<>Already have an account? {linkButton('Sign in', toSignIn)}</>}
        />
      )}
      {flow.name === 'forgot' && (
        <ForgotPasswordForm
          logo={<Logo />}
          onSuccess={toSignIn}
          initialEmail={flow.email}
          startAtReset={flow.startAtReset}
          autoSignIn
          signInLink={linkButton('Back to sign in', toSignIn)}
        />
      )}
    </div>
  )
}

/** Signed in, but the stack refuses this account (403): say why, offer a way out. */
function NoAccess({
  who,
  requiredGroup,
  onSignOut
}: {
  who: string | undefined
  requiredGroup: string | null
  onSignOut: () => void
}) {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-4 py-10">
      <Card className="w-full max-w-md" data-testid="no-access">
        <CardBody className="space-y-4">
          <Logo />
          <h1 className="text-lg font-semibold text-foreground">No access to this stack yet</h1>
          <p className="text-sm text-muted-foreground">
            {who ? (
              <>
                You are signed in as <strong className="text-foreground">{who}</strong>, but this
                account has not been granted access to Nimbus here.
              </>
            ) : (
              <>This account has not been granted access to Nimbus here.</>
            )}{' '}
            {requiredGroup ? (
              <>
                Ask the stack&apos;s owner to add you to the{' '}
                <code className="font-mono">{requiredGroup}</code> group.
              </>
            ) : (
              <>Ask the stack&apos;s owner for access.</>
            )}
          </p>
          <Button variant="secondary" onClick={onSignOut}>
            Sign out
          </Button>
        </CardBody>
      </Card>
    </div>
  )
}
