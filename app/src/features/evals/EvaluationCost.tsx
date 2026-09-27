/**
 * What an evaluation spent, split into the model under test and the judge,
 * plus a notice when its `max_cost_usd` budget stopped it early.
 *
 * Renders nothing for results recorded before costs were tracked.
 */

import { Alert } from '@readysetcloud/ui'
import type { EvaluationResult } from '../../api'
import { COST_ESTIMATE_NOTE, formatUsd } from '../../components/cost'

function CostFigure({ label, value, testId }: { label: string; value: unknown; testId: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-mono text-foreground tabular-nums" data-testid={testId}>
        {formatUsd(value)}
      </dd>
    </div>
  )
}

export default function EvaluationCost({ result }: { result: EvaluationResult | null }) {
  const cost = result?.cost
  if (!result || !cost) return null
  return (
    <section aria-labelledby="eval-cost-heading" data-testid="eval-cost" title={COST_ESTIMATE_NOTE}>
      <h3 id="eval-cost-heading" className="text-sm font-semibold text-foreground mb-2">
        Cost <span className="font-normal text-xs text-muted-foreground">(estimate)</span>
      </h3>
      {result.budget_exhausted && (
        <Alert variant="info" className="mb-3" data-testid="eval-budget-exhausted">
          The {formatUsd(cost.max_cost_usd)} budget was reached, so {result.skipped_runs ?? 0} runs
          were not started. The result covers the runs that did happen.
        </Alert>
      )}
      <dl className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <CostFigure label="Total" value={cost.total_usd} testId="eval-cost-total" />
        <CostFigure label="Runs" value={cost.runs_usd} testId="eval-cost-runs" />
        <CostFigure label="Judge" value={cost.judge_usd} testId="eval-cost-judge" />
        {cost.max_cost_usd !== null && cost.max_cost_usd !== undefined && (
          <CostFigure label="Budget" value={cost.max_cost_usd} testId="eval-cost-budget" />
        )}
      </dl>
    </section>
  )
}
