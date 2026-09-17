/**
 * Cognito auth core — the framework-agnostic half.
 *
 * The pattern is readysetcloud/rsc-core's `@readysetcloud/ui/auth`, trimmed to
 * what this app needs: a session document in localStorage
 * (`{idToken, refreshToken, expiresAt}`) and plain `cognito-idp` calls over
 * fetch. No Hosted UI, no SDK, no OAuth redirect — the sign-in form posts
 * straight to the user pool's API and the ID token goes to our server as a
 * bearer header (see `setAuthTokenProvider` in `../api/http`).
 *
 * Deliberately absent, versus rsc-core: sign-up (the pool is invitation-only,
 * see api/template.yaml `UserPool`) and the cross-subdomain cookie bridge
 * (there is one origin).
 *
 * Which pool to talk to is not baked in at build time: the server publishes
 * it from `GET /health` (`auth.region` / `auth.client_id`) and `AuthGate`
 * calls `configureAuth` with it before anything else runs.
 */

/** localStorage key for the session document. Bump on incompatible change. */
export const AUTH_STORAGE_KEY = 'evalharness.auth.v1'

export interface AuthConfig {
  region: string
  clientId: string
}

export interface Session {
  idToken: string
  refreshToken?: string
  /** Epoch seconds. */
  expiresAt: number
}

export interface IdClaims {
  email?: string
  sub?: string
  [claim: string]: unknown
}

export type SignInResult = { kind: 'success' } | { kind: 'newPasswordRequired'; session: string }

/* ---------- configuration ---------- */

let config: AuthConfig | null = null

export function configureAuth(next: AuthConfig | null): void {
  config = next
}

export function getAuthConfig(): AuthConfig | null {
  return config
}

/* ---------- errors ---------- */

/** Cognito exception name -> copy a person should read. */
const ERROR_COPY: Record<string, string> = {
  NotAuthorizedException: 'Incorrect email or password.',
  UserNotFoundException: 'Incorrect email or password.',
  InvalidPasswordException: "That password doesn't meet the requirements.",
  InvalidParameterException: 'Please check what you entered and try again.',
  CodeMismatchException: "That code isn't right — check it and try again.",
  ExpiredCodeException: 'That code has expired — request a new one.',
  LimitExceededException: 'Too many attempts — wait a few minutes and try again.',
  TooManyRequestsException: 'Too many attempts — wait a moment and try again.',
  PasswordResetRequiredException: 'Your password must be reset before you can sign in.',
  UserNotConfirmedException: "This account hasn't verified its email yet."
}

function errorCode(body: { __type?: unknown }): string {
  return (
    String(body.__type ?? '')
      .split('#')
      .pop() ?? ''
  ).replace(/:.*$/, '')
}

/** Exported for tests: raw Cognito error body -> user-facing message. */
export function errorMessage(body: { __type?: unknown; message?: unknown }): string {
  return (
    ERROR_COPY[errorCode(body)] ||
    (typeof body.message === 'string' ? body.message : '') ||
    'Something went wrong — please try again.'
  )
}

export class AuthError extends Error {
  readonly code: string
  constructor(message: string, code: string) {
    super(message)
    this.name = 'AuthError'
    this.code = code
  }
}

export const isAuthError = (e: unknown): e is AuthError => e instanceof AuthError

/* ---------- the session document ---------- */

const nowSec = () => Math.floor(Date.now() / 1000)

export function readSession(): Session | null {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY)
    if (!raw) return null
    const doc = JSON.parse(raw) as Partial<Session> | null
    if (!doc || typeof doc.idToken !== 'string' || typeof doc.expiresAt !== 'number') return null
    if (doc.refreshToken !== undefined && typeof doc.refreshToken !== 'string') return null
    return doc as Session
  } catch {
    return null
  }
}

export const isSignedIn = (): boolean => readSession() !== null

/** Decoded ID-token payload; `{}` when signed out or unreadable. */
export function claims(): IdClaims {
  const session = readSession()
  if (!session) return {}
  try {
    const payload = session.idToken.split('.')[1]!.replace(/-/g, '+').replace(/_/g, '/')
    return JSON.parse(atob(payload)) as IdClaims
  } catch {
    return {}
  }
}

/* ---------- change notification (React re-renders + cross-tab) ---------- */

const listeners = new Set<() => void>()

export function onAuthChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function notify(): void {
  for (const listener of [...listeners]) {
    try {
      listener()
    } catch {
      /* one bad listener never blocks the rest */
    }
  }
}

/** A sign-in/out in another tab updates this one. Idempotent. */
let storageListenerInstalled = false
export function installStorageListener(): void {
  if (storageListenerInstalled || typeof window === 'undefined') return
  storageListenerInstalled = true
  window.addEventListener('storage', e => {
    if (e.key === AUTH_STORAGE_KEY || e.key === null) notify()
  })
}

interface CognitoAuthResult {
  IdToken?: string
  RefreshToken?: string
  ExpiresIn?: number
}

function saveAuthResult(result: CognitoAuthResult | undefined): void {
  if (!result?.IdToken) return
  const prev = readSession()
  const session: Session = {
    idToken: result.IdToken,
    refreshToken: result.RefreshToken || prev?.refreshToken,
    expiresAt: nowSec() + (result.ExpiresIn || 3600)
  }
  try {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session))
  } catch {
    /* storage unavailable — assertSessionPersisted reports it */
  }
  notify()
}

/**
 * A Cognito call that succeeded is not a sign-in that succeeded if the
 * browser refused to keep the tokens (private mode, "block all site data").
 * Without this the form would re-render untouched with no error.
 */
function assertSessionPersisted(): void {
  if (readSession()) return
  throw new AuthError(
    "This browser isn't keeping you signed in. Allow site data for this site, then try again.",
    'SessionNotPersisted'
  )
}

function clearSession(): void {
  try {
    localStorage.removeItem(AUTH_STORAGE_KEY)
  } catch {
    /* ignore */
  }
  notify()
}

/* ---------- Cognito user pool API ---------- */

interface InitiateAuthResponse {
  AuthenticationResult?: CognitoAuthResult
  ChallengeName?: string
  Session?: string
}

function requireConfig(): AuthConfig {
  if (!config) {
    throw new AuthError('Sign-in is not configured for this deployment.', 'ConfigMissing')
  }
  return config
}

async function idp<T>(region: string, action: string, payload: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
      method: 'POST',
      headers: {
        'content-type': 'application/x-amz-json-1.1',
        'x-amz-target': `AWSCognitoIdentityProviderService.${action}`
      },
      body: JSON.stringify(payload)
    })
  } catch {
    throw new AuthError(
      'Could not reach the sign-in service. Check your connection.',
      'NetworkError'
    )
  }
  const body = (await res.json().catch(() => ({}))) as { __type?: unknown; message?: unknown }
  if (!res.ok) throw new AuthError(errorMessage(body), errorCode(body) || `Http${res.status}`)
  return body as T
}

/* ---------- operations ---------- */

export async function signIn(email: string, password: string): Promise<SignInResult> {
  const { region, clientId } = requireConfig()
  const out = await idp<InitiateAuthResponse>(region, 'InitiateAuth', {
    AuthFlow: 'USER_PASSWORD_AUTH',
    ClientId: clientId,
    AuthParameters: { USERNAME: email, PASSWORD: password }
  })
  if (out.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
    return { kind: 'newPasswordRequired', session: out.Session ?? '' }
  }
  saveAuthResult(out.AuthenticationResult)
  assertSessionPersisted()
  return { kind: 'success' }
}

/** Answer the NEW_PASSWORD_REQUIRED challenge an admin-created user gets first. */
export async function completeNewPassword(
  email: string,
  newPassword: string,
  session: string
): Promise<void> {
  const { region, clientId } = requireConfig()
  const out = await idp<InitiateAuthResponse>(region, 'RespondToAuthChallenge', {
    ClientId: clientId,
    ChallengeName: 'NEW_PASSWORD_REQUIRED',
    Session: session,
    ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword }
  })
  saveAuthResult(out.AuthenticationResult)
  assertSessionPersisted()
}

export async function forgotPassword(email: string): Promise<void> {
  const { region, clientId } = requireConfig()
  await idp(region, 'ForgotPassword', { ClientId: clientId, Username: email })
}

export async function confirmForgotPassword(
  email: string,
  code: string,
  newPassword: string
): Promise<void> {
  const { region, clientId } = requireConfig()
  await idp(region, 'ConfirmForgotPassword', {
    ClientId: clientId,
    Username: email,
    ConfirmationCode: code,
    Password: newPassword
  })
}

/**
 * A valid ID token, refreshed behind the scenes when it is within a minute of
 * expiry. A definite Cognito rejection clears the session; a network failure
 * keeps it (being offline should not sign anyone out) and yields `null`.
 */
export async function getFreshIdToken(): Promise<string | null> {
  const session = readSession()
  if (!session) return null
  if (session.expiresAt - 60 > nowSec()) return session.idToken
  if (!session.refreshToken || !config) {
    clearSession()
    return null
  }
  try {
    const out = await idp<InitiateAuthResponse>(config.region, 'InitiateAuth', {
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: config.clientId,
      AuthParameters: { REFRESH_TOKEN: session.refreshToken }
    })
    saveAuthResult(out.AuthenticationResult)
    return readSession()?.idToken ?? null
  } catch (e) {
    if (isAuthError(e) && e.code !== 'NetworkError') clearSession()
    return null
  }
}

/** Drop the session, then best-effort revoke the refresh token. */
export async function signOut(): Promise<void> {
  const session = readSession()
  clearSession()
  if (!session?.refreshToken || !config) return
  try {
    await idp(config.region, 'RevokeToken', {
      ClientId: config.clientId,
      Token: session.refreshToken
    })
  } catch {
    /* best effort */
  }
}
