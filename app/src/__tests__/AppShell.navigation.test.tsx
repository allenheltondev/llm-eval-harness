/**
 * AppShell writes a page's own selection into the address bar: picking an
 * evaluation or a run (or clearing it) is a new route. The pages are stubbed
 * down to the callback they are handed -- their own behavior is their tests'.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../features/evals/EvalsPage', () => ({
  default: ({ onSelectEvaluation }: { onSelectEvaluation: (id: string | null) => void }) => (
    <div>
      <button type="button" onClick={() => onSelectEvaluation('eval-7')}>
        pick evaluation
      </button>
      <button type="button" onClick={() => onSelectEvaluation(null)}>
        clear evaluation
      </button>
    </div>
  )
}))

vi.mock('../features/history/HistoryPage', () => ({
  default: ({ onSelectRun }: { onSelectRun: (id: string | null) => void }) => (
    <div>
      <button type="button" onClick={() => onSelectRun('run-3')}>
        pick run
      </button>
      <button type="button" onClick={() => onSelectRun(null)}>
        clear run
      </button>
    </div>
  )
}))

const { default: AppShell } = await import('../AppShell')

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})

function go(hash: string) {
  act(() => {
    window.history.replaceState(null, '', `/${hash}`)
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
}

describe('AppShell selection routes', () => {
  it('puts the picked evaluation in the address bar, and takes it out again', () => {
    render(<AppShell />)
    go('#/evals')

    fireEvent.click(screen.getByRole('button', { name: 'pick evaluation' }))
    expect(window.location.hash).toBe('#/evals/eval-7')

    fireEvent.click(screen.getByRole('button', { name: 'clear evaluation' }))
    expect(window.location.hash).toBe('#/evals')
  })

  it('puts the picked run in the address bar, and takes it out again', () => {
    render(<AppShell />)
    go('#/history')

    fireEvent.click(screen.getByRole('button', { name: 'pick run' }))
    expect(window.location.hash).toBe('#/runs/run-3')

    fireEvent.click(screen.getByRole('button', { name: 'clear run' }))
    expect(window.location.hash).toBe('#/history')
  })
})

/**
 * AppNav's mobile menu (the collapsible top bar below 641px) holds its own
 * open state and does not close when a link is followed. AppShell closes it.
 */
describe('AppShell mobile menu', () => {
  function openMenu() {
    const toggle = screen.getByRole('button', { name: 'Toggle navigation' })
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Toggle navigation' })).toHaveAttribute(
      'aria-expanded',
      'true'
    )
  }

  function expectMenuClosed() {
    expect(screen.getByRole('button', { name: 'Toggle navigation' })).toHaveAttribute(
      'aria-expanded',
      'false'
    )
    expect(document.querySelector('.app-nav-collapse-open')).toBeNull()
  }

  async function follow(name: string) {
    const link = screen.getByRole('link', { name })
    await act(async () => {
      fireEvent.click(link)
      // jsdom follows the fragment link; the hash router hears hashchange.
      await new Promise(resolve => setTimeout(resolve, 0))
    })
  }

  it('closes after following a link to another page', async () => {
    render(<AppShell />)
    go('#/workbench')
    openMenu()

    await follow('History')

    expect(window.location.hash).toBe('#/history')
    await waitFor(() =>
      expect(screen.getByRole('link', { name: 'History' })).toHaveAttribute('aria-current', 'page')
    )
    expectMenuClosed()
  })

  it('closes after tapping the page already shown', async () => {
    render(<AppShell />)
    go('#/evals')
    openMenu()

    await follow('Evals')

    expectMenuClosed()
  })

  it('closes after tapping Workbench from a bare address, which changes no route', async () => {
    render(<AppShell />)
    openMenu()

    await follow('Workbench')

    expectMenuClosed()
  })

  it('closes when the route changes some other way (back, a page selection)', () => {
    render(<AppShell />)
    go('#/evals')
    openMenu()

    fireEvent.click(screen.getByRole('button', { name: 'pick evaluation' }))

    expectMenuClosed()
  })
})
