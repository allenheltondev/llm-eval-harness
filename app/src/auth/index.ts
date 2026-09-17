/** Public surface of the auth layer (see core.ts for the design). */

export {
  AUTH_STORAGE_KEY,
  AuthError,
  claims,
  completeNewPassword,
  configureAuth,
  confirmForgotPassword,
  errorMessage,
  forgotPassword,
  getAuthConfig,
  getFreshIdToken,
  isAuthError,
  isSignedIn,
  onAuthChange,
  readSession,
  signIn,
  signOut,
  type AuthConfig,
  type IdClaims,
  type Session,
  type SignInResult
} from './core'
export { AuthProvider, useAuth, type AuthState } from './react'
export { default as AuthGate, SESSION_EXPIRED_NOTICE, type AuthGateProps } from './AuthGate'
export { default as LoginPage, PASSWORD_REQUIREMENTS, type LoginPageProps } from './LoginPage'
