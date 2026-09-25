/**
 * AppShell: the AppNav rail mounts exactly one page at a time, and every section in
 * `TABS` resolves to a real page (a missing case in `TabPage` would render
 * nothing and fail here rather than silently showing a blank tab).
 *
 * The Workbench's catalog loaders are stubbed at the store level and its
 * `GET /tools` fetch at the API client — this is a shell test, not a network
 * test.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'

const toolsMock = vi.fn()
const getEvaluationMock = vi.fn()

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      tools: toolsMock,
      evaluations: { ...actual.api.evaluations, get: getEvaluationMock }
    }
  }
})

const { default: AppShell, TABS } = await import('../AppShell')
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

/** Test ids rendered by each page, keyed by tab. */
const PAGE_TEST_IDS: Record<string, string> = {
  workbench: 'workbench-page',
  evals: 'evals-page',
  history: 'history-page',
  guardrails: 'guardrails-page',
  about: 'about-page'
}

beforeEach(() => {
  // The route is the URL fragment, which outlives a test in jsdom; so does the
  // theme AppNav writes onto <html>.
  window.history.replaceState(null, '', '/')
  delete document.documentElement.dataset.theme
  getEvaluationMock.mockReset()
  getEvaluationMock.mockReturnValue(new Promise(() => {}))
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

describe('AppShell', () => {
  /** The rail's link for a section. */
  const link = (name: string) =>
    within(screen.getByRole('navigation', { name: 'Primary navigation' })).getByRole('link', {
      name
    })

  it('renders the Nimbus rail with exactly the five sections, and no Scenarios', () => {
    render(<AppShell />)

    expect(screen.getByText('Nimbus')).toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Primary navigation' })
    expect(
      within(nav)
        .getAllByRole('link')
        .map(item => item.textContent)
    ).toEqual(['Workbench', 'Evals', 'History', 'Guardrails', 'About'])
    expect(within(nav).queryByRole('link', { name: 'Scenarios' })).not.toBeInTheDocument()
  })

  it('groups the sections under their headings', () => {
    render(<AppShell />)

    const nav = screen.getByRole('navigation', { name: 'Primary navigation' })
    const titles = Array.from(nav.querySelectorAll('.app-nav-section-title'), el => el.textContent)
    expect(titles).toEqual(['Run', 'Review', 'Manage'])
  })

  it('opens on the Workbench', () => {
    render(<AppShell />)

    expect(screen.getByTestId('workbench-page')).toBeInTheDocument()
    expect(link('Workbench')).toHaveAttribute('aria-current', 'page')
  })

  it.each(TABS.map(tab => [tab.id, tab.label] as const))(
    'following the %s link mounts its page and nothing else',
    (id, label) => {
      render(<AppShell />)

      expect(link(label)).toHaveAttribute('href', `#/${id}`)
      act(() => {
        window.history.replaceState(null, '', `/#/${id}`)
        window.dispatchEvent(new HashChangeEvent('hashchange'))
      })

      expect(screen.getByTestId(PAGE_TEST_IDS[id])).toBeInTheDocument()
      expect(link(label)).toHaveAttribute('aria-current', 'page')

      // Every other page is unmounted, and no other link is current.
      for (const [otherId, testId] of Object.entries(PAGE_TEST_IDS)) {
        if (otherId === id) continue
        expect(screen.queryByTestId(testId)).not.toBeInTheDocument()
      }
      expect(
        within(screen.getByRole('navigation', { name: 'Primary navigation' }))
          .getAllByRole('link')
          .filter(item => item.getAttribute('aria-current') === 'page')
      ).toHaveLength(1)
    }
  )

  it('labels the main region with the current section', () => {
    window.history.replaceState(null, '', '/#/about')
    render(<AppShell />)

    expect(screen.getByRole('main', { name: 'About' })).toHaveAttribute('id', 'section-about')
  })

  it.each(TABS.map(tab => [tab.id, tab]))('opens %s under a page hero naming it', (_id, tab) => {
    window.history.replaceState(null, '', `/#/${tab.id}`)
    render(<AppShell />)

    const main = screen.getByRole('main', { name: tab.label })
    // The only h1 on the page, so every page has exactly one title.
    const titles = within(main).getAllByRole('heading', { level: 1 })
    expect(titles).toHaveLength(1)
    expect(titles[0]).toHaveTextContent(tab.label)
    expect(within(main).getByText(tab.description)).toBeInTheDocument()
  })

  it('renders no mascot, companion or bring-back control', () => {
    render(<AppShell />)

    expect(screen.queryByTestId('robot-graphic')).not.toBeInTheDocument()
    expect(screen.queryByTestId('floating-chad')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Bring back Chad' })).not.toBeInTheDocument()
  })

  it('opens the page a link names', () => {
    window.history.replaceState(null, '', '/#/evals/eval-1')
    render(<AppShell />)

    expect(screen.getByTestId('evals-page')).toBeInTheDocument()
    expect(link('Evals')).toHaveAttribute('aria-current', 'page')
    expect(getEvaluationMock).toHaveBeenCalledWith('eval-1')
  })

  it('opens History for a run link', () => {
    window.history.replaceState(null, '', '/#/runs/run-9')
    render(<AppShell />)

    expect(screen.getByTestId('history-page')).toBeInTheDocument()
  })

  it('points the brand at the Workbench', () => {
    render(<AppShell />)

    expect(screen.getByRole('link', { name: 'Nimbus' })).toHaveAttribute('href', '#/workbench')
  })

  it('follows the address bar (back button, pasted link)', () => {
    render(<AppShell />)

    act(() => {
      window.history.replaceState(null, '', '/#/about')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })

    expect(screen.getByTestId('about-page')).toBeInTheDocument()
  })

  it('remembers the theme the toggle picks', () => {
    render(<AppShell />)

    fireEvent.click(screen.getByRole('button', { name: /Switch to (dark|light) theme/ }))

    const chosen = localStorage.getItem('nimbus.theme')
    expect(chosen === 'dark' || chosen === 'light').toBe(true)
    expect(document.documentElement.dataset.theme).toBe(chosen)
  })

  it('starts from the remembered theme', () => {
    localStorage.setItem('nimbus.theme', 'dark')
    render(<AppShell />)

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(screen.getByRole('button', { name: 'Switch to light theme' })).toBeInTheDocument()
  })

  it('still themes when storage is unavailable (private mode)', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    try {
      render(<AppShell />)

      fireEvent.click(screen.getByRole('button', { name: /Switch to (dark|light) theme/ }))

      expect(document.documentElement.dataset.theme).toMatch(/^(dark|light)$/)
    } finally {
      getItem.mockRestore()
      setItem.mockRestore()
    }
  })

  it('shows no sign-in controls or app launcher when the stack has no sign-in', () => {
    render(<AppShell />)

    expect(screen.queryByRole('button', { name: 'Open profile menu' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open app launcher' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Sign in' })).not.toBeInTheDocument()
  })
})
