/**
 * The rail's account controls: absent whenever the deployment has no auth
 * (every local run); behind sign-in, the profile menu (who is signed in, and
 * sign-out) and the Ready, Set, Cloud app launcher.
 *
 * The session is supplied through the app's own context -- AuthGate fills it
 * from the auth package in the real app; its behavior is AuthGate's tests.
 */

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const toolsMock = vi.fn()

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
})

const { default: AppShell } = await import('../AppShell')
const { SessionContext } = await import('../auth')
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

type SessionValue = import('../auth').Session

function signedIn(overrides: Partial<SessionValue> = {}): SessionValue {
  return {
    required: true,
    signedIn: true,
    user: { email: 'ada@example.com', given_name: 'Ada', family_name: 'Lovelace' },
    signOut: vi.fn().mockResolvedValue(undefined),
    ...overrides
  }
}

function renderWith(session: SessionValue) {
  return render(
    <SessionContext.Provider value={session}>
      <AppShell />
    </SessionContext.Provider>
  )
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
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
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AppShell account controls', () => {
  it('renders none outside sign-in (every local run)', () => {
    render(<AppShell />)

    expect(screen.queryByRole('button', { name: 'Open profile menu' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
  })

  it('shows who is signed in and signs out from the profile menu', async () => {
    const session = signedIn()
    renderWith(session)

    fireEvent.click(screen.getByRole('button', { name: 'Open profile menu' }))
    const menu = screen.getByRole('dialog', { name: 'Profile menu' })
    expect(within(menu).getByRole('heading', { name: 'Ada Lovelace' })).toBeInTheDocument()
    expect(within(menu).getByText('ada@example.com')).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(within(menu).getByRole('button', { name: 'Sign out' }))
    })
    expect(session.signOut).toHaveBeenCalledTimes(1)
  })

  it('names an account by its email when the token carries no name', () => {
    renderWith(signedIn({ user: { email: 'grace@example.com' } }))

    fireEvent.click(screen.getByRole('button', { name: 'Open profile menu' }))
    const menu = screen.getByRole('dialog', { name: 'Profile menu' })
    expect(within(menu).getByRole('heading', { name: 'grace@example.com' })).toBeInTheDocument()
  })

  it('opens the Ready, Set, Cloud app launcher', () => {
    renderWith(signedIn())

    fireEvent.click(screen.getByRole('button', { name: 'Open app launcher' }))
    const launcher = screen.getByRole('dialog', { name: 'App launcher' })
    const hrefs = within(launcher)
      .getAllByRole('link')
      .map(link => link.getAttribute('href'))
    expect(hrefs).toContain('https://readysetcloud.io')
    expect(hrefs).toContain('https://bootcamp.readysetcloud.io')
  })
})
