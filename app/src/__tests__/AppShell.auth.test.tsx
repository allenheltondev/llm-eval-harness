/**
 * The header's sign-out control: absent whenever the deployment has no auth
 * (every local run), present and working behind an AuthProvider.
 *
 * The Workbench's `GET /tools` fetch is stubbed at the API client so the only
 * `fetch` the sign-out test observes is Cognito's RevokeToken.
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, mockFetch } from '../api/__tests__/helpers'

const toolsMock = vi.fn()

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
})

const { default: AppShell } = await import('../AppShell')
const { AUTH_STORAGE_KEY, AuthProvider, configureAuth, readSession } = await import('../auth')
const {
  DEFAULT_RUN_CONFIG,
  DEFAULT_SETTINGS,
  INITIAL_RUN_STATE,
  useGuardrailStore,
  useModelStore,
  useRunConfigStore,
  useRunStore,
  useSettingsStore
} = await import('../stores')

function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `h.${b64(JSON.stringify(payload))}.s`
}

beforeEach(() => {
  localStorage.clear()
  toolsMock.mockReset()
  toolsMock.mockResolvedValue({ toolsets: [] })
  useRunStore.setState({ ...INITIAL_RUN_STATE })
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useSettingsStore.setState({ ...DEFAULT_SETTINGS })
  useModelStore.setState({
    models: [],
    loadModels: vi.fn().mockResolvedValue(undefined)
  })
  useGuardrailStore.setState({
    guardrails: [],
    loadGuardrails: vi.fn().mockResolvedValue(undefined)
  })
  configureAuth({ region: 'us-east-1', clientId: 'c' })
})

afterEach(() => {
  vi.unstubAllGlobals()
  configureAuth(null)
})

describe('AppShell sign-out', () => {
  it('renders no sign-out control outside an AuthProvider', () => {
    render(<AppShell />)
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
  })

  it('renders no sign-out control behind a provider while signed out', () => {
    render(
      <AuthProvider>
        <AppShell />
      </AuthProvider>
    )
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
  })

  it('shows who is signed in and clears the session on click', async () => {
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: fakeJwt({ email: 'a@b.c' }), refreshToken: 'rt', expiresAt: 9e9 })
    )
    const spy = mockFetch(jsonResponse({}))
    render(
      <AuthProvider>
        <AppShell />
      </AuthProvider>
    )
    const button = screen.getByRole('button', { name: 'Sign out' })
    expect(button).toHaveAttribute('title', 'Signed in as a@b.c')

    await act(async () => {
      fireEvent.click(button)
    })
    expect(readSession()).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
    expect(spy).toHaveBeenCalledTimes(1) // RevokeToken
  })

  it('falls back to a plain title when the token carries no email', () => {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: fakeJwt({}), expiresAt: 9e9 }))
    render(
      <AuthProvider>
        <AppShell />
      </AuthProvider>
    )
    expect(screen.getByRole('button', { name: 'Sign out' })).toHaveAttribute('title', 'Sign out')
  })
})
