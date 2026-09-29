/**
 * ReadinessPanel: can each candidate stand in for the baseline? The verdict, the
 * reasons, the cases broken and fixed, and how much of it can be told from chance.
 */

import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import type {
  BaselineAssessment,
  ComparisonArm,
  ComparisonBar,
  ModelComparison
} from '../../../api'
import ReadinessPanel from '../ReadinessPanel'

function arm(label: string, overrides: Partial<ComparisonArm> = {}): ComparisonArm {
  return {
    label,
    provider: 'bedrock',
    model_id: label,
    evaluation_id: `eval-${label}`,
    status: 'completed',
    score: 80,
    grade: 'B',
    pass_rate: 1,
    cases_passed: 40,
    cases_total: 40,
    cases_errored: 0,
    repeats: 3,
    cost_usd: 1,
    budget_exhausted: false,
    name: label,
    prompt_id: null,
    baseline: false,
    assertions_failed: null,
    latency_p50_ms: 400,
    latency_p95_ms: 1000,
    changes: [],
    ...overrides
  }
}

function assessment(overrides: Partial<BaselineAssessment> = {}): BaselineAssessment {
  return {
    status: 'ready',
    reasons: [
      '0 regression(s) in 40 baseline-passing cases; a regression rate above 7% is ruled out'
    ],
    regressions: [],
    improvements: [],
    critical_regressions: [],
    baseline_passed: 40,
    regression_rate: 0,
    regression_upper_bound: 0.07,
    cases_needed: 29,
    sign_test_p: 1,
    pass_rate_delta: 0,
    score_delta: 0,
    latency_ratio: 1.2,
    cost_ratio: 0.4,
    changes: ['model'],
    ...overrides
  }
}

function comparison(
  candidate: Partial<BaselineAssessment> | null,
  // `null` means the server sent no bar; leaving it out means an ordinary suite bar.
  bar: ComparisonBar | null = {
    max_regression_rate: 0.1,
    max_latency_ratio: null,
    max_cost_ratio: null,
    source: 'suite'
  },
  extra: Partial<ModelComparison> = {}
): ModelComparison {
  return {
    arms: [
      arm('primary', { baseline: true }),
      arm('fallback', { vs_baseline: candidate === null ? null : assessment(candidate) })
    ],
    ranking: ['primary', 'fallback'],
    winner: null,
    tied: [],
    cases: [],
    split_cases: [],
    suite: { name: 'support', cases: 40 },
    baseline: 'primary',
    warnings: [],
    ...(bar ? { bar } : {}),
    ...extra
  }
}

describe('ReadinessPanel', () => {
  it('asks the question in terms of the baseline', () => {
    render(<ReadinessPanel comparison={comparison({})} />)

    expect(
      screen.getByRole('heading', { name: 'Can these stand in for primary?' })
    ).toBeInTheDocument()
  })

  it('renders nothing without a baseline', () => {
    const { container } = render(
      <ReadinessPanel comparison={comparison({}, null, { baseline: null })} />
    )

    expect(container).toBeEmptyDOMElement()
  })

  it.each([
    ['ready', 'Ready'],
    ['not_ready', 'Not ready'],
    ['inconclusive', 'Inconclusive']
  ] as const)('shows a %s candidate as "%s"', (status, label) => {
    render(<ReadinessPanel comparison={comparison({ status })} />)

    expect(screen.getByTestId('readiness-status-fallback')).toHaveTextContent(label)
  })

  it('gives every reason for the verdict', () => {
    render(
      <ReadinessPanel
        comparison={comparison({ status: 'not_ready', reasons: ['too slow', 'too dear'] })}
      />
    )

    const reasons = within(screen.getByTestId('readiness-reasons-fallback')).getAllByRole(
      'listitem'
    )
    expect(reasons.map(item => item.textContent)).toEqual(['too slow', 'too dear'])
  })

  it('says what the candidate changes from the baseline', () => {
    render(<ReadinessPanel comparison={comparison({ changes: ['model', 'prompt'] })} />)

    expect(screen.getByTestId('readiness-changes-fallback')).toHaveTextContent(
      'differs from the baseline in model and prompt'
    )
  })

  it('says so when the candidate is the baseline with nothing changed', () => {
    render(<ReadinessPanel comparison={comparison({ changes: [] })} />)

    expect(screen.getByText('same settings as the baseline')).toBeInTheDocument()
  })

  it('shows the deltas against the baseline', () => {
    render(
      <ReadinessPanel
        comparison={comparison({
          pass_rate_delta: -0.05,
          score_delta: -3,
          latency_ratio: 1.8,
          cost_ratio: 0.4
        })}
      />
    )

    const panel = screen.getByTestId('readiness-fallback')
    expect(panel).toHaveTextContent('−5 pts')
    expect(panel).toHaveTextContent('−3')
    expect(panel).toHaveTextContent('1.8×')
    expect(panel).toHaveTextContent('0.4×')
  })

  it('shows unmeasured deltas as dashes, not zeros', () => {
    render(
      <ReadinessPanel
        comparison={comparison({
          pass_rate_delta: null,
          score_delta: null,
          latency_ratio: null,
          cost_ratio: null
        })}
      />
    )

    expect(within(screen.getByTestId('readiness-fallback')).getAllByText('—')).toHaveLength(4)
  })

  it('lists the cases it broke, marking a critical one', () => {
    render(
      <ReadinessPanel
        comparison={comparison(
          {
            status: 'not_ready',
            regressions: ['hours', 'refund'],
            critical_regressions: ['refund']
          },
          undefined,
          {
            cases: [
              { id: 'hours', critical: false, results: {}, agreement: 'split' },
              { id: 'refund', critical: true, results: {}, agreement: 'split' }
            ]
          }
        )}
      />
    )

    expect(screen.getByTestId('readiness-case-broke-hours')).not.toHaveTextContent('critical')
    expect(screen.getByTestId('readiness-case-broke-refund')).toHaveTextContent('critical')
  })

  it('lists the cases it fixed', () => {
    render(<ReadinessPanel comparison={comparison({ improvements: ['greeting'] })} />)

    expect(screen.getByTestId('readiness-case-fixed-greeting')).toBeInTheDocument()
  })

  it('shows neither list when nothing differs', () => {
    render(<ReadinessPanel comparison={comparison({})} />)

    expect(screen.queryByText('Broke')).not.toBeInTheDocument()
    expect(screen.queryByText('Fixed')).not.toBeInTheDocument()
  })

  it('states the regression rate and its 95% bound', () => {
    render(
      <ReadinessPanel
        comparison={comparison({
          regressions: ['a', 'b'],
          baseline_passed: 40,
          regression_rate: 0.05,
          regression_upper_bound: 0.15
        })}
      />
    )

    expect(screen.getByTestId('readiness-bound-fallback')).toHaveTextContent(
      '2 of 40 baseline-passing cases regressed (5%); at 95% confidence the true rate is at most 15%.'
    )
  })

  it('does not state a rate when the baseline passes nothing', () => {
    render(
      <ReadinessPanel comparison={comparison({ regression_rate: null, baseline_passed: 0 })} />
    )

    expect(screen.queryByTestId('readiness-bound-fallback')).not.toBeInTheDocument()
  })

  it('says a difference a sign test cannot tell from chance is not yet evidence', () => {
    render(
      <ReadinessPanel
        comparison={comparison({ regressions: ['a'], improvements: ['b'], sign_test_p: 1 })}
      />
    )

    expect(screen.getByTestId('readiness-fallback')).toHaveTextContent(
      'differs from the baseline on 2 cases, which a sign test cannot tell from chance (p=1.00)'
    )
  })

  it('says a difference unlikely to be chance is', () => {
    render(
      <ReadinessPanel
        comparison={comparison({
          regressions: [],
          improvements: ['a', 'b', 'c', 'd', 'e', 'f'],
          sign_test_p: 0.03
        })}
      />
    )

    expect(screen.getByTestId('readiness-fallback')).toHaveTextContent(
      'unlikely to be chance (sign test p=0.03)'
    )
  })

  it('says a single differing case in the singular', () => {
    render(<ReadinessPanel comparison={comparison({ improvements: ['a'], sign_test_p: 1 })} />)

    expect(screen.getByTestId('readiness-fallback')).toHaveTextContent('on 1 case, which')
  })

  it('says why a candidate cannot be measured when the baseline never finished', () => {
    render(<ReadinessPanel comparison={comparison(null)} />)

    expect(screen.getByTestId('readiness-fallback')).toHaveTextContent(
      "Cannot be measured: primary's evaluation did not complete."
    )
  })

  it.each([
    ['suite', "the suite's bar"],
    ['default', 'the default bar: the suite sets none'],
    ['requested', 'the requested bar']
  ] as const)('says where a %s bar came from', (source, text) => {
    render(
      <ReadinessPanel
        comparison={comparison(
          {},
          {
            max_regression_rate: 0.1,
            max_latency_ratio: null,
            max_cost_ratio: null,
            source
          }
        )}
      />
    )

    expect(screen.getByTestId('readiness-bar')).toHaveTextContent(text)
  })

  it('states the whole bar, and only the limits that are set', () => {
    render(
      <ReadinessPanel
        comparison={comparison(
          {},
          {
            max_regression_rate: 0.05,
            max_latency_ratio: 1.5,
            max_cost_ratio: 2,
            source: 'suite'
          }
        )}
      />
    )

    const bar = screen.getByTestId('readiness-bar')
    expect(bar).toHaveTextContent('at most 5% of the cases the baseline passes may regress')
    expect(bar).toHaveTextContent('p95 latency within 1.5×')
    expect(bar).toHaveTextContent('cost within 2×')
    expect(bar).toHaveTextContent('Baseline p95 latency 1.0 s')
  })

  it('omits the limits that are not set', () => {
    render(<ReadinessPanel comparison={comparison({})} />)

    const bar = screen.getByTestId('readiness-bar')
    expect(bar).not.toHaveTextContent('latency within')
    expect(bar).not.toHaveTextContent('cost within')
  })

  it('shows no bar line when the server sent none', () => {
    render(<ReadinessPanel comparison={comparison({}, null)} />)

    expect(screen.queryByTestId('readiness-bar')).not.toBeInTheDocument()
  })

  it('has a card for every candidate', () => {
    const many = comparison({})
    many.arms.push(arm('third', { vs_baseline: assessment({ status: 'not_ready' }) }))

    render(<ReadinessPanel comparison={many} />)

    expect(screen.getByTestId('readiness-fallback')).toBeInTheDocument()
    expect(screen.getByTestId('readiness-third')).toBeInTheDocument()
    expect(screen.queryByTestId('readiness-primary')).not.toBeInTheDocument()
  })
})
