/**
 * Public surface of the auth layer. Sign-in itself is the Ready, Set, Cloud
 * auth package (`@readysetcloud/ui/auth`); this layer decides *whether* the
 * app needs it (AuthGate) and what the app knows about the user (session).
 */

export { default as AuthGate, SESSION_EXPIRED_NOTICE, type AuthGateProps } from './AuthGate'
export { NO_AUTH, SessionContext, displayName, useSession, type Session } from './session'
