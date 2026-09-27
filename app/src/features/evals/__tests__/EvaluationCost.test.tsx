/**
 * EvaluationCost: an evaluation's spend, split into the runs and the judge,
 * and a notice when its budget stopped it before every run started.
 */

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import EvaluationCost from '../EvaluationCost'
import type { EvaluationCost as Cost, EvaluationResult } from '../../../api'

function result(cost?: Cost, extra: Partial<EvaluationResult> = {}): EvaluationResult {
  return {
    grade: 'A',
    score: 95,
    reasoning: null,
    judge: { model_id: 'amazon.nova-pro-v1:0', system_prompt_used: false, rubric_used: false },
    metrics: {},
    run_ids: ['r1'],
    failed_runs: [],
    ...(cost ? { cost } : {}),
    ...extra
  }
}

const COST: Cost = {
  currency: 'USD',
  estimate: true,
  pricing_as_of: '2026-09-27',
  runs_usd: 2,
  judge_usd: 0.0123,
  total_usd: 2.0123,
  max_cost_usd: null
}

describe('EvaluationCost', () => {
  it('renders nothing without a result, or for one recorded before costs existed', () => {
    const { container, rerender } = render(<EvaluationCost result={null} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<EvaluationCost result={result()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('splits the spend between the runs and the judge', () => {
    render(<EvaluationCost result={result(COST)} />)

    expect(screen.getByTestId('eval-cost')).toHaveTextContent('(estimate)')
    expect(screen.getByTestId('eval-cost-total')).toHaveTextContent('$2.01')
    expect(screen.getByTestId('eval-cost-runs')).toHaveTextContent('$2.00')
    expect(screen.getByTestId('eval-cost-judge')).toHaveTextContent('$0.0123')
    expect(screen.queryByTestId('eval-cost-budget')).not.toBeInTheDocument()
    expect(screen.queryByTestId('eval-budget-exhausted')).not.toBeInTheDocument()
  })

  it('shows an unpriced model as unknown rather than free', () => {
    render(<EvaluationCost result={result({ ...COST, runs_usd: null, total_usd: null })} />)

    expect(screen.getByTestId('eval-cost-total')).toHaveTextContent('unknown')
    expect(screen.getByTestId('eval-cost-runs')).toHaveTextContent('unknown')
  })

  it('says when the budget stopped new runs from starting', () => {
    render(
      <EvaluationCost
        result={result({ ...COST, max_cost_usd: 2.5 }, { budget_exhausted: true, skipped_runs: 3 })}
      />
    )

    expect(screen.getByTestId('eval-cost-budget')).toHaveTextContent('$2.50')
    expect(screen.getByTestId('eval-budget-exhausted')).toHaveTextContent(
      'The $2.50 budget was reached, so 3 runs were not started.'
    )
  })

  it('counts no skipped runs when an older result did not record them', () => {
    render(
      <EvaluationCost result={result({ ...COST, max_cost_usd: 1 }, { budget_exhausted: true })} />
    )
    expect(screen.getByTestId('eval-budget-exhausted')).toHaveTextContent('so 0 runs')
  })
})
