/**
 * The sign-in screen: email + password, the NEW_PASSWORD_REQUIRED step an
 * invited user hits on first sign-in, and the forgot-password flow.
 *
 * Purely presentational over `../auth/core`; on success the core notifies
 * `AuthProvider`, which re-renders the gate into the app — nothing here
 * navigates.
 */

import { useState, type FormEvent } from 'react'
import {
  completeNewPassword,
  confirmForgotPassword,
  forgotPassword,
  isAuthError,
  signIn
} from './core'

type Step =
  | { kind: 'signIn' }
  | { kind: 'newPassword'; session: string }
  | { kind: 'forgot' }
  | { kind: 'reset' }

export const PASSWORD_REQUIREMENTS =
  'At least 8 characters, with an uppercase letter, a lowercase letter and a number.'

export interface LoginPageProps {
  /** A one-line notice above the form (e.g. "Your session expired"). */
  notice?: string | null
}

function messageOf(error: unknown): string {
  if (isAuthError(error)) return error.message
  return 'Something went wrong — please try again.'
}

export default function LoginPage({ notice = null }: LoginPageProps) {
  const [step, setStep] = useState<Step>({ kind: 'signIn' })
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(notice)

  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setBusy(false)
    }
  }

  function handleSignIn(event: FormEvent) {
    event.preventDefault()
    void run(async () => {
      const result = await signIn(email.trim(), password)
      if (result.kind === 'newPasswordRequired') {
        setInfo('Choose a new password to finish setting up your account.')
        setStep({ kind: 'newPassword', session: result.session })
      }
    })
  }

  function handleNewPassword(event: FormEvent) {
    event.preventDefault()
    if (step.kind !== 'newPassword') return
    const { session } = step
    void run(() => completeNewPassword(email.trim(), newPassword, session))
  }

  function handleForgot(event: FormEvent) {
    event.preventDefault()
    void run(async () => {
      await forgotPassword(email.trim())
      setInfo('If that account exists, a reset code is on its way to its email.')
      setStep({ kind: 'reset' })
    })
  }

  function handleReset(event: FormEvent) {
    event.preventDefault()
    void run(async () => {
      await confirmForgotPassword(email.trim(), code.trim(), newPassword)
      setInfo('Password updated — sign in with it below.')
      setPassword('')
      setNewPassword('')
      setCode('')
      setStep({ kind: 'signIn' })
    })
  }

  function goTo(next: Step) {
    setError(null)
    setInfo(null)
    setStep(next)
  }

  const inputClass =
    'w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500'
  const buttonClass =
    'w-full rounded-md bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-60'
  const linkClass = 'text-sm text-primary-700 hover:underline'

  const emailField = (
    <label className="block">
      <span className="text-sm font-medium text-gray-700">Email</span>
      <input
        type="email"
        name="email"
        autoComplete="username"
        required
        value={email}
        onChange={e => setEmail(e.target.value)}
        className={inputClass}
      />
    </label>
  )

  const newPasswordField = (
    <div>
      <label className="block">
        <span className="text-sm font-medium text-gray-700">New password</span>
        <input
          type="password"
          name="newPassword"
          autoComplete="new-password"
          aria-describedby="new-password-help"
          required
          value={newPassword}
          onChange={e => setNewPassword(e.target.value)}
          className={inputClass}
        />
      </label>
      <p id="new-password-help" className="mt-1 text-xs text-gray-500">
        {PASSWORD_REQUIREMENTS}
      </p>
    </div>
  )

  return (
    <div
      className="min-h-screen bg-gradient-to-br from-primary-50 to-secondary-100 flex items-center justify-center px-4"
      data-testid="login-page"
    >
      <div className="w-full max-w-sm rounded-lg border border-secondary-200 bg-surface p-6 shadow-md">
        <div className="mb-5">
          <h1 className="text-lg font-bold text-primary-700 leading-tight">Nimbus</h1>
          <p className="text-xs text-secondary-700">Sign in to continue</p>
        </div>

        {info && (
          <p
            role="status"
            className="mb-3 rounded-md bg-secondary-100 px-3 py-2 text-sm text-secondary-800"
          >
            {info}
          </p>
        )}
        {error && (
          <p role="alert" className="mb-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        {step.kind === 'signIn' && (
          <form onSubmit={handleSignIn} className="space-y-3" aria-label="Sign in">
            {emailField}
            <label className="block">
              <span className="text-sm font-medium text-gray-700">Password</span>
              <input
                type="password"
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={e => setPassword(e.target.value)}
                className={inputClass}
              />
            </label>
            <button type="submit" disabled={busy} className={buttonClass}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
            <div className="text-center">
              <button type="button" className={linkClass} onClick={() => goTo({ kind: 'forgot' })}>
                Forgot your password?
              </button>
            </div>
          </form>
        )}

        {step.kind === 'newPassword' && (
          <form onSubmit={handleNewPassword} className="space-y-3" aria-label="Set a new password">
            {newPasswordField}
            <button type="submit" disabled={busy} className={buttonClass}>
              {busy ? 'Saving…' : 'Set password and sign in'}
            </button>
          </form>
        )}

        {step.kind === 'forgot' && (
          <form onSubmit={handleForgot} className="space-y-3" aria-label="Reset your password">
            {emailField}
            <button type="submit" disabled={busy} className={buttonClass}>
              {busy ? 'Sending…' : 'Send reset code'}
            </button>
            <div className="text-center">
              <button type="button" className={linkClass} onClick={() => goTo({ kind: 'signIn' })}>
                Back to sign in
              </button>
            </div>
          </form>
        )}

        {step.kind === 'reset' && (
          <form onSubmit={handleReset} className="space-y-3" aria-label="Choose a new password">
            <label className="block">
              <span className="text-sm font-medium text-gray-700">Reset code</span>
              <input
                type="text"
                name="code"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                value={code}
                onChange={e => setCode(e.target.value)}
                className={inputClass}
              />
            </label>
            {newPasswordField}
            <button type="submit" disabled={busy} className={buttonClass}>
              {busy ? 'Updating…' : 'Update password'}
            </button>
            <div className="text-center">
              <button type="button" className={linkClass} onClick={() => goTo({ kind: 'signIn' })}>
                Back to sign in
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
