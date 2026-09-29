/**
 * ModelComparisonView: the verdict (a winner, or a tie — never a win on level
 * quality), the ranking table, and the case grid where models disagree.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ComparisonArm, ModelComparison } from '../../../api'

const compareMock = vi.fn()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return {
    ...actual,
    api: { ...actual.api, evaluations: { ...actual.api.evaluations, compare: compareMock } }
  }
})

const ModelComparisonView = (await import('../ModelComparisonView')).default

function arm(model: string, overrides: Partial<ComparisonArm> = {}): ComparisonArm {
  return {
    label: `bedrock:${model}`,
    provider: 'bedrock',
    model_id: model,
    evaluation_id: `eval-${model}`,
    status: 'completed',
    score: 80,
    grade: 'B',
    pass_rate: 1,
    cases_passed: 4,
    cases_total: 4,
    cases_errored: 0,
    repeats: 1,
    cost_usd: 0.0123,
    budget_exhausted: false,
    name: null,
    prompt_id: null,
    baseline: false,
    assertions_failed: null,
    latency_p50_ms: null,
    latency_p95_ms: null,
    changes: [],
    ...overrides
  }
}

const passed = { status: 'passed', score: 1 }
const failed = { status: 'failed', score: 0.2 }

function comparison(overrides: Partial<ModelComparison> = {}): ModelComparison {
  return {
    arms: [
      arm('weak', { pass_rate: 0.5, cases_passed: 2, score: 55, grade: 'D', cost_usd: 0.5 }),
      arm('strong')
    ],
    ranking: ['bedrock:strong', 'bedrock:weak'],
    winner: 'bedrock:strong',
    tied: [],
    cases: [
      {
        id: 'both-pass',
        critical: false,
        results: { 'bedrock:strong': passed, 'bedrock:weak': passed },
        agreement: 'all_passed'
      },
      {
        id: 'strong-only',
        critical: false,
        results: { 'bedrock:strong': passed, 'bedrock:weak': failed },
        agreement: 'split'
      }
    ],
    split_cases: ['strong-only'],
    suite: { name: 'support', cases: 4 },
    baseline: null,
    warnings: [],
    ...overrides
  }
}

async function shown(value: ModelComparison, ids = ['eval-weak', 'eval-strong']) {
  compareMock.mockResolvedValue(value)
  const view = render(<ModelComparisonView evaluationIds={ids} onClose={vi.fn()} />)
  await screen.findByTestId('model-compare-verdict')
  return view
}

beforeEach(() => {
  compareMock.mockReset()
})

describe('ModelComparisonView', () => {
  it('asks the API to compare the given evaluations, and says it is working', async () => {
    compareMock.mockReturnValue(new Promise(() => {}))

    render(<ModelComparisonView evaluationIds={['a', 'b']} />)

    expect(compareMock).toHaveBeenCalledWith(['a', 'b'], expect.anything())
    expect(screen.getByTestId('model-compare-loading')).toBeInTheDocument()
  })

  it('names the winner and lists the models best first', async () => {
    await shown(comparison())

    expect(screen.getByTestId('model-compare-verdict')).toHaveTextContent(
      'bedrock:strong did best.'
    )
    const rows = within(screen.getByTestId('compare-arms')).getAllByRole('row').slice(1)
    expect(rows[0]).toHaveTextContent('bedrock:strong')
    expect(rows[0]).toHaveTextContent('Winner')
    expect(rows[0]).toHaveTextContent('4/4 (100%)')
    expect(rows[0]).toHaveTextContent('80 B')
    expect(rows[0]).toHaveTextContent('$0.0123')
    expect(rows[1]).toHaveTextContent('bedrock:weak')
    expect(rows[1]).toHaveTextContent('2/4 (50%)')
  })

  it('links each model to its own evaluation', async () => {
    await shown(comparison())

    const row = screen.getByTestId('compare-arm-bedrock:strong')
    expect(within(row).getByRole('link', { name: 'Open' })).toHaveAttribute(
      'href',
      '#/evals/eval-strong'
    )
  })

  it('calls level quality a tie, not a win', async () => {
    await shown(
      comparison({
        arms: [arm('x'), arm('y')],
        ranking: ['bedrock:x', 'bedrock:y'],
        winner: null,
        tied: ['bedrock:x', 'bedrock:y']
      })
    )

    expect(screen.getByTestId('model-compare-verdict')).toHaveTextContent(
      'A tie: bedrock:x and bedrock:y are level'
    )
    expect(screen.queryByText('Winner')).not.toBeInTheDocument()
    expect(screen.getAllByText('Tied')).toHaveLength(2)
  })

  it('says so when no model completed', async () => {
    await shown(
      comparison({
        arms: [arm('x', { status: 'error', pass_rate: null, cases_passed: null, score: null })],
        ranking: [],
        winner: null,
        tied: [],
        cases: [],
        split_cases: []
      })
    )

    expect(screen.getByTestId('model-compare-verdict')).toHaveTextContent(
      'No model completed, so there is nothing to rank.'
    )
  })

  it('lists a model that did not complete as not ranked, with its status', async () => {
    await shown(
      comparison({
        arms: [arm('done'), arm('cut', { status: 'cancelled', cases_passed: 1, pass_rate: 0.25 })],
        ranking: ['bedrock:done'],
        winner: 'bedrock:done'
      })
    )

    const row = screen.getByTestId('compare-arm-bedrock:cut')
    expect(row).toHaveTextContent('Not ranked (cancelled)')
    expect(row).toHaveTextContent('1/4 (25%) so far')
    // Its partial score is not shown beside the ranked models' scores.
    expect(row).not.toHaveTextContent('80 B')
    expect(within(row).getAllByRole('cell')[0]).toHaveTextContent('—')
  })

  it('shows a model with no result at all as a dash', async () => {
    await shown(
      comparison({
        arms: [arm('done'), arm('empty', { status: 'error', pass_rate: null, cases_passed: null })],
        ranking: ['bedrock:done'],
        winner: 'bedrock:done'
      })
    )

    expect(screen.getByTestId('compare-arm-bedrock:empty')).toHaveTextContent('Not ranked (error)')
  })

  it('says when a model was stopped by its budget', async () => {
    await shown(comparison({ arms: [arm('weak', { budget_exhausted: true }), arm('strong')] }))

    expect(screen.getByTestId('compare-arm-bedrock:weak')).toHaveTextContent(
      'Stopped by its budget'
    )
  })

  it('shows an unpriced model as unknown, not free', async () => {
    await shown(comparison({ arms: [arm('weak', { cost_usd: null }), arm('strong')] }))

    expect(screen.getByTestId('compare-arm-bedrock:weak')).toHaveTextContent('unknown')
  })

  it('warns that a one-case gap is noise, but not a wide one', async () => {
    const { unmount } = await shown(
      comparison({
        arms: [arm('a', { cases_passed: 3 }), arm('b', { cases_passed: 4 })],
        ranking: ['bedrock:b', 'bedrock:a']
      })
    )
    expect(screen.getByTestId('model-compare-narrow')).toBeInTheDocument()
    unmount()

    await shown(comparison()) // 4 vs 2
    expect(screen.queryByTestId('model-compare-narrow')).not.toBeInTheDocument()
  })

  it('does not warn about a gap with only one ranked model', async () => {
    await shown(comparison({ ranking: ['bedrock:strong'], arms: [arm('strong')] }))

    expect(screen.queryByTestId('model-compare-narrow')).not.toBeInTheDocument()
  })

  it("shows every case with each model's verdict, disagreements marked", async () => {
    await shown(comparison())

    const split = screen.getByTestId('compare-case-strong-only')
    expect(split).toHaveTextContent('Pass')
    expect(split).toHaveTextContent('Fail')
    expect(split).toHaveTextContent('Models disagree')
    expect(screen.getByTestId('compare-case-both-pass')).toHaveTextContent('All passed')
  })

  it('narrows the grid to the cases the models disagree on', async () => {
    await shown(comparison())

    fireEvent.click(screen.getByTestId('model-compare-split-only'))

    expect(screen.getByTestId('compare-case-strong-only')).toBeInTheDocument()
    expect(screen.queryByTestId('compare-case-both-pass')).not.toBeInTheDocument()
  })

  it('says the models agree when there is nothing to show', async () => {
    await shown(comparison({ split_cases: [], cases: [comparison().cases[0]] }))

    fireEvent.click(screen.getByTestId('model-compare-split-only'))

    expect(screen.getByTestId('compare-no-disagreements')).toBeInTheDocument()
  })

  it('shows a case a model never ran, and one it could not judge', async () => {
    await shown(
      comparison({
        cases: [
          {
            id: 'odd',
            critical: false,
            results: {
              'bedrock:strong': { status: 'judge_error', score: null },
              'bedrock:weak': null
            },
            agreement: 'incomplete'
          }
        ],
        split_cases: []
      })
    )

    const row = screen.getByTestId('compare-case-odd')
    expect(row).toHaveTextContent('Not judged')
    expect(row).toHaveTextContent('Not run')
    expect(row).toHaveTextContent('Incomplete')
  })

  it('shows an unrecognised case status as it came', async () => {
    await shown(
      comparison({
        cases: [
          {
            id: 'new',
            critical: false,
            results: { 'bedrock:strong': { status: 'skipped', score: null }, 'bedrock:weak': null },
            agreement: 'incomplete'
          }
        ],
        split_cases: []
      })
    )

    expect(screen.getByTestId('compare-case-new')).toHaveTextContent('skipped')
  })

  it('shows unranked models after the ranked ones in the case grid', async () => {
    await shown(
      comparison({
        arms: [arm('done'), arm('cut', { status: 'cancelled' })],
        ranking: ['bedrock:done'],
        winner: 'bedrock:done',
        cases: [
          {
            id: 'c',
            critical: false,
            results: { 'bedrock:done': passed, 'bedrock:cut': passed },
            agreement: 'all_passed'
          }
        ],
        split_cases: []
      })
    )

    const headings = within(screen.getByTestId('compare-cases')).getAllByRole('columnheader')
    expect(headings.map(cell => cell.textContent)).toEqual([
      'Case',
      'bedrock:done',
      'bedrock:cut',
      'Result'
    ])
  })

  it('shows the suite name and size in the header', async () => {
    await shown(comparison())

    expect(screen.getByRole('heading', { name: /Compare models/ })).toHaveTextContent(
      'support · 4 cases'
    )
  })

  it('leaves the suite out of the header when it has no name', async () => {
    await shown(comparison({ suite: { name: null, cases: 4 } }))

    expect(screen.getByRole('heading', { name: /Compare models/ })).not.toHaveTextContent('cases')
  })

  it('reports why a set cannot be compared', async () => {
    compareMock.mockRejectedValue(new Error('These evaluations did not run the same suite'))

    render(<ModelComparisonView evaluationIds={['a', 'b']} />)

    expect(await screen.findByTestId('model-compare-error')).toHaveTextContent(
      'did not run the same suite'
    )
    expect(screen.queryByTestId('model-compare-loading')).not.toBeInTheDocument()
  })

  it('reports a failure that is not an Error', async () => {
    compareMock.mockRejectedValue('offline')

    render(<ModelComparisonView evaluationIds={['a', 'b']} />)

    expect(await screen.findByTestId('model-compare-error')).toHaveTextContent('offline')
  })

  it('has a close button only when it can be closed', async () => {
    const onClose = vi.fn()
    compareMock.mockResolvedValue(comparison())
    const { unmount } = render(<ModelComparisonView evaluationIds={['a', 'b']} onClose={onClose} />)

    fireEvent.click(await screen.findByTestId('model-compare-close'))
    expect(onClose).toHaveBeenCalledTimes(1)
    unmount()

    render(<ModelComparisonView evaluationIds={['a', 'b']} />)
    await waitFor(() => expect(screen.getByTestId('model-compare-verdict')).toBeInTheDocument())
    expect(screen.queryByTestId('model-compare-close')).not.toBeInTheDocument()
  })

  it('compares again for different evaluations, but not for the same ids in a new array', async () => {
    compareMock.mockResolvedValue(comparison())
    const { rerender } = render(<ModelComparisonView evaluationIds={['a', 'b']} />)
    await screen.findByTestId('model-compare-verdict')

    rerender(<ModelComparisonView evaluationIds={['a', 'b']} />)
    expect(compareMock).toHaveBeenCalledTimes(1)

    rerender(<ModelComparisonView evaluationIds={['a', 'c']} />)
    await waitFor(() => expect(compareMock).toHaveBeenCalledTimes(2))
  })

  it('ignores an answer that arrives after the comparison was closed', async () => {
    let reject: (reason: Error) => void = () => {}
    compareMock.mockReturnValue(
      new Promise((_resolve, fail) => {
        reject = fail
      })
    )
    const { unmount } = render(<ModelComparisonView evaluationIds={['a', 'b']} />)
    unmount()

    reject(new Error('late'))

    await Promise.resolve()
    expect(screen.queryByTestId('model-compare-error')).not.toBeInTheDocument()
  })
})

describe('ModelComparisonView: as a fallback check', () => {
  const assessment = {
    status: 'not_ready' as const,
    reasons: ['1 of the 2 cases the baseline passes now fails (50%), over the 10% limit'],
    regressions: ['strong-only'],
    improvements: [],
    critical_regressions: [],
    baseline_passed: 2,
    regression_rate: 0.5,
    regression_upper_bound: 0.9,
    cases_needed: 29,
    sign_test_p: 1,
    pass_rate_delta: -0.5,
    score_delta: -25,
    latency_ratio: 2,
    cost_ratio: 0.5,
    changes: ['model', 'prompt']
  }

  function withBaseline(overrides: Partial<ModelComparison> = {}): ModelComparison {
    return comparison({
      arms: [
        arm('strong', {
          name: 'primary',
          label: 'primary',
          model_id: 'claude-x',
          prompt_id: 'ab12cd34',
          baseline: true,
          latency_p95_ms: 1200
        }),
        arm('weak', {
          name: 'fallback',
          label: 'fallback',
          pass_rate: 0.5,
          cases_passed: 2,
          latency_p95_ms: 2400,
          vs_baseline: assessment,
          changes: ['model', 'prompt']
        })
      ],
      ranking: ['primary', 'fallback'],
      winner: 'primary',
      baseline: 'primary',
      bar: {
        max_regression_rate: 0.1,
        max_latency_ratio: null,
        max_cost_ratio: null,
        source: 'suite'
      },
      cases: [
        {
          id: 'strong-only',
          critical: true,
          results: { primary: passed, fallback: failed },
          agreement: 'split'
        },
        {
          id: 'both-pass',
          critical: false,
          results: { primary: passed, fallback: passed },
          agreement: 'all_passed'
        }
      ],
      ...overrides
    })
  }

  it('shows the readiness panel once there is a baseline', async () => {
    await shown(withBaseline())

    expect(screen.getByTestId('readiness-panel')).toBeInTheDocument()
    expect(screen.getByTestId('readiness-status-fallback')).toHaveTextContent('Not ready')
  })

  it('shows no readiness panel when it only ranks', async () => {
    await shown(comparison())

    expect(screen.queryByTestId('readiness-panel')).not.toBeInTheDocument()
    expect(screen.queryByText('As a fallback')).not.toBeInTheDocument()
  })

  it('marks the baseline and shows the verdict for the others in the table', async () => {
    await shown(withBaseline())

    expect(screen.getByTestId('compare-arm-primary')).toHaveTextContent('Baseline')
    const fallbackRow = screen.getByTestId('compare-arm-fallback')
    expect(within(fallbackRow).getByText('Not ready')).toBeInTheDocument()
    // The baseline has no verdict of its own.
    expect(within(screen.getByTestId('compare-arm-primary')).queryByText('Not ready')).toBeNull()
  })

  it('shows p95 latency when any arm has it', async () => {
    await shown(withBaseline())

    expect(screen.getByTestId('compare-arm-primary')).toHaveTextContent('1.2 s')
    expect(screen.getByTestId('compare-arm-fallback')).toHaveTextContent('2.4 s')
  })

  it('has no latency column for results that predate it', async () => {
    await shown(comparison())

    expect(screen.queryByText('p95 latency')).not.toBeInTheDocument()
  })

  it('shows what a named arm is made of under its name', async () => {
    await shown(withBaseline())

    expect(screen.getByTestId('compare-arm-primary')).toHaveTextContent('bedrock:claude-x')
    expect(screen.getByTestId('compare-arm-primary')).toHaveTextContent('prompt ab12cd34')
  })

  it('highlights the cells where the baseline passes and a candidate does not', async () => {
    await shown(withBaseline())

    const regressed = screen
      .getByTestId('compare-case-strong-only')
      .querySelectorAll('[data-regression]')
    expect(regressed).toHaveLength(1)
    expect(regressed[0]).toHaveTextContent('Fail')
    expect(regressed[0]).toHaveTextContent('regressed from the baseline')
    expect(
      screen.getByTestId('compare-case-both-pass').querySelectorAll('[data-regression]')
    ).toHaveLength(0)
  })

  it('does not call a case a regression when the baseline itself did not pass it', async () => {
    await shown(
      withBaseline({
        cases: [
          {
            id: 'never',
            critical: false,
            results: { primary: failed, fallback: failed },
            agreement: 'all_failed'
          }
        ]
      })
    )

    expect(
      screen.getByTestId('compare-case-never').querySelectorAll('[data-regression]')
    ).toHaveLength(0)
  })

  it('does not highlight anything without a baseline', async () => {
    await shown(comparison())

    expect(document.querySelectorAll('[data-regression]')).toHaveLength(0)
  })

  it('marks the cases a fallback must not break', async () => {
    await shown(withBaseline())

    expect(screen.getByTestId('compare-critical-strong-only')).toBeInTheDocument()
    expect(screen.queryByTestId('compare-critical-both-pass')).not.toBeInTheDocument()
  })

  it('labels the baseline column in the grid', async () => {
    await shown(withBaseline())

    const headings = within(screen.getByTestId('compare-cases')).getAllByRole('columnheader')
    expect(headings.map(cell => cell.textContent)).toContain('primary (baseline)')
  })

  it('shows the warnings', async () => {
    await shown(
      withBaseline({
        warnings: [{ code: 'small_suite', message: 'The suite has fewer than 20 cases.', arms: [] }]
      })
    )

    expect(screen.getByTestId('warning-small_suite')).toBeInTheDocument()
  })

  it('shows the effects of a grid', async () => {
    await shown(
      withBaseline({
        effects: {
          axes: { model: [{ value: 'a', mean_pass_rate: 0.7, arms: 2 }] },
          spread: { model: 0.2 },
          dominant: 'model'
        }
      })
    )

    expect(screen.getByTestId('effects-panel')).toBeInTheDocument()
  })

  it('shows no effects panel without a grid', async () => {
    await shown(withBaseline())

    expect(screen.queryByTestId('effects-panel')).not.toBeInTheDocument()
  })

  it('offers each arm as a baseline and starts on the one the server chose', async () => {
    await shown(withBaseline())

    const picker = screen.getByTestId('model-compare-baseline') as HTMLSelectElement
    expect(
      within(picker)
        .getAllByRole('option')
        .map(option => option.textContent)
    ).toEqual(['primary', 'fallback'])
    expect(picker.value).toBe('eval-strong')
  })

  it('asks the server again, with the chosen baseline, when the reader picks another', async () => {
    await shown(withBaseline())
    compareMock.mockResolvedValue(withBaseline({ baseline: 'fallback' }))

    fireEvent.change(screen.getByTestId('model-compare-baseline'), {
      target: { value: 'eval-weak' }
    })

    await waitFor(() => expect(compareMock).toHaveBeenCalledTimes(2))
    expect(compareMock.mock.calls[1][1]).toMatchObject({ baseline: 'eval-weak' })
    await screen.findByTestId('model-compare-verdict')
  })

  it('invites the reader to choose a baseline when there is none', async () => {
    await shown(comparison({ arms: [arm('weak'), arm('strong')] }))

    const picker = screen.getByTestId('model-compare-baseline') as HTMLSelectElement
    expect(picker.value).toBe('')
    expect(within(picker).getByRole('option', { name: 'Choose a baseline…' })).toBeInTheDocument()
    expect(screen.getByText(/to see what each other arm breaks/)).toBeInTheDocument()
  })

  it('ignores the placeholder being chosen again', async () => {
    await shown(comparison({ arms: [arm('weak'), arm('strong')] }))

    fireEvent.change(screen.getByTestId('model-compare-baseline'), { target: { value: '' } })

    expect(compareMock).toHaveBeenCalledTimes(1)
  })

  it('forgets the chosen baseline when the evaluations being compared change', async () => {
    compareMock.mockResolvedValue(withBaseline())
    const { rerender } = render(<ModelComparisonView evaluationIds={['a', 'b']} />)
    await screen.findByTestId('model-compare-baseline')
    fireEvent.change(screen.getByTestId('model-compare-baseline'), {
      target: { value: 'eval-weak' }
    })
    await waitFor(() => expect(compareMock).toHaveBeenCalledTimes(2))

    rerender(<ModelComparisonView evaluationIds={['c', 'd']} />)

    await waitFor(() =>
      expect(compareMock.mock.calls.at(-1)?.[1]).toMatchObject({ baseline: undefined })
    )
  })

  it('clips its scrolling tables, so hidden text in a far column cannot widen the page', async () => {
    await shown(withBaseline())

    // An absolutely positioned element (the "regressed" note) escapes an overflow clip unless
    // the scroll container is the containing block. jsdom has no layout, so the class is checked.
    for (const testId of ['compare-arms', 'compare-cases']) {
      const container = screen.getByTestId(testId).parentElement
      expect(container).toHaveClass('overflow-x-auto', 'relative')
    }
  })
})
