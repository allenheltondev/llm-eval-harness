/**
 * AppShell: the tab bar mounts exactly one page at a time, and every tab in
 * `TABS` resolves to a real page (a missing case in `TabPage` would render
 * nothing and fail here rather than silently showing a blank tab).
 *
 * The Workbench's catalog loaders are stubbed at the store level and its
 * `GET /tools` fetch at the API client — this is a shell test, not a network
 * test.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

const toolsMock = vi.fn()

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
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
  it('renders the header title and exactly the five tabs, with no Scenarios tab', () => {
    render(<AppShell />)

    expect(screen.getByRole('heading', { name: 'LLM Eval Harness' })).toBeInTheDocument()

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map(tab => tab.textContent)).toEqual([
      'Workbench',
      'Evals',
      'History',
      'Guardrails',
      'About'
    ])
    expect(screen.queryByRole('tab', { name: 'Scenarios' })).not.toBeInTheDocument()
  })

  it('opens on the Workbench', () => {
    render(<AppShell />)

    expect(screen.getByTestId('workbench-page')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Workbench' })).toHaveAttribute('aria-selected', 'true')
  })

  it.each(TABS.map(tab => [tab.id, tab.label] as const))(
    'switching to %s mounts its page and nothing else',
    (id, label) => {
      render(<AppShell />)

      fireEvent.click(screen.getByRole('tab', { name: label }))

      expect(screen.getByTestId(PAGE_TEST_IDS[id])).toBeInTheDocument()
      expect(screen.getByRole('tab', { name: label })).toHaveAttribute('aria-selected', 'true')

      // Every other page is unmounted.
      for (const [otherId, testId] of Object.entries(PAGE_TEST_IDS)) {
        if (otherId === id) continue
        expect(screen.queryByTestId(testId)).not.toBeInTheDocument()
      }
    }
  )

  it('points the tabpanel at the active tab', () => {
    render(<AppShell />)

    fireEvent.click(screen.getByRole('tab', { name: 'About' }))

    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveAttribute('id', 'tabpanel-about')
    expect(panel).toHaveAttribute('aria-labelledby', 'tab-about')
  })

  it('renders no mascot, companion or bring-back control', () => {
    render(<AppShell />)

    expect(screen.queryByTestId('robot-graphic')).not.toBeInTheDocument()
    expect(screen.queryByTestId('floating-chad')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Bring back Chad' })).not.toBeInTheDocument()
  })
})
