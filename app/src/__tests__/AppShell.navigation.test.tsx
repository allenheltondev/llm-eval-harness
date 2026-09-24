/**
 * AppShell writes a page's own selection into the address bar: picking an
 * evaluation or a run (or clearing it) is a new route. The pages are stubbed
 * down to the callback they are handed -- their own behavior is their tests'.
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
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
