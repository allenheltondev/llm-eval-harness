/**
 * The assembled `EvaluationResult`: grade, score, reasoning, judge info, the
 * local + judge metrics grid, and any failed runs.
 *
 * Takes the result as a prop rather than reading `evalStore` itself, so the
 * same component renders both the just-finished active evaluation
 * (`evalStore.result`) and a selected past evaluation (`EvaluationDetail.result`
 * from the list/`refreshEvaluation`).
 */

import { Alert, Badge, Card, CardBody, StatTile } from '@readysetcloud/ui'
import type { EvaluationResult } from '../../api'

/** A/B -> success, C/D -> warning, F -> error. Anything else (a stray "Pass"/"Fail" grade) is neutral. */
const GRADE_COLORS: Record<string, string> = {
  A: 'text-success-600',
  B: 'text-success-600',
  C: 'text-warning-600',
  D: 'text-warning-600',
  F: 'text-error-600'
}

function gradeColorClass(grade: string | null): string {
  if (!grade) return 'text-muted-foreground'
  return GRADE_COLORS[grade.trim().charAt(0).toUpperCase()] ?? 'text-muted-foreground'
}

/** The judge's per-run scores, when there are enough to draw a line through. */
function perRunScores(metrics: Record<string, unknown>): number[] | undefined {
  const scores = metrics.judge_scores
  if (!Array.isArray(scores) || scores.length < 2) return undefined
  return scores.every(score => typeof score === 'number') ? (scores as number[]) : undefined
}

const METRIC_LABELS: Record<string, string> = {
  runs_analyzed: 'Runs analyzed',
  exact_match_count: 'Exact matches',
  unique_outputs: 'Unique outputs',
  output_length_variance: 'Output length variance',
  tool_sequence_consistency: 'Tool sequence consistency',
  modal_tool_sequence: 'Modal tool sequence',
  judge_overall_score: 'Judge overall score',
  judge_scores: 'Judge scores',
  judge_pass_rate: 'Judge pass rate',
  tool_consistency_judge_score: 'Tool consistency (judge)'
}

/** Order the known metrics consistently; anything unrecognized is skipped. */
const METRIC_ORDER = Object.keys(METRIC_LABELS)

function formatMetric(value: unknown): string {
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (Array.isArray(value)) return value.length === 0 ? '—' : value.join(', ')
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function describeFailure(error: Record<string, unknown> | null): string {
  if (!error) return 'unknown error'
  if (typeof error.message === 'string' && error.message !== '') return error.message
  if (typeof error.code === 'string' && error.code !== '') return error.code
  return 'unknown error'
}

interface EvalResultViewProps {
  result: EvaluationResult
}

export default function EvalResultView({ result }: EvalResultViewProps) {
  const metricEntries = METRIC_ORDER.filter(key => key in result.metrics).map(
    key => [key, result.metrics[key]] as const
  )

  const sparkline = perRunScores(result.metrics)

  return (
    <Card role="region" aria-labelledby="eval-result-heading" data-testid="eval-result">
      <CardBody>
        <h3 id="eval-result-heading" className="sr-only">
          Evaluation result
        </h3>

        <div className="grid grid-cols-2 gap-3 mb-4">
          <StatTile
            label="Grade"
            value={
              <span className={gradeColorClass(result.grade)} data-testid="eval-grade">
                {result.grade ?? '—'}
              </span>
            }
          />
          <StatTile
            label="Score"
            value={
              <span data-testid="eval-score">
                {result.score === null ? '—' : `${result.score} / 100`}
              </span>
            }
            meta={sparkline ? 'Judge score per run' : undefined}
            sparkline={sparkline}
          />
        </div>

        {result.reasoning && (
          <p
            className="text-sm text-muted-foreground whitespace-pre-wrap mb-4"
            data-testid="eval-reasoning"
          >
            {result.reasoning}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2 mb-4">
          <Badge variant="neutral">Judge: {result.judge.model_id}</Badge>
          {result.judge.system_prompt_used && <Badge variant="primary">Custom prompt</Badge>}
          {result.judge.rubric_used && <Badge variant="primary">Custom rubric</Badge>}
        </div>

        {result.judge_error && (
          <Alert variant="error" className="mb-4">
            Judge error: {result.judge_error}
          </Alert>
        )}

        {metricEntries.length > 0 && (
          <dl
            className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-4 p-3 rounded-lg border border-border bg-muted/30"
            data-testid="eval-metrics-grid"
          >
            {metricEntries.map(([key, value]) => (
              <div key={key} className="min-w-0">
                <dt className="text-xs text-muted-foreground">{METRIC_LABELS[key]}</dt>
                <dd
                  className="text-sm font-medium text-foreground truncate"
                  title={formatMetric(value)}
                >
                  {formatMetric(value)}
                </dd>
              </div>
            ))}
          </dl>
        )}

        {result.failed_runs.length > 0 && (
          <div className="mb-4" data-testid="eval-failed-runs">
            <h4 className="text-xs font-medium text-muted-foreground mb-1">
              Failed runs ({result.failed_runs.length})
            </h4>
            <ul className="text-xs text-error-700 space-y-0.5">
              {result.failed_runs.map(failure => (
                <li key={failure.index}>
                  Run {failure.index + 1}: {describeFailure(failure.error)}
                </li>
              ))}
            </ul>
          </div>
        )}

        {result.run_ids.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {result.run_ids.length} run{result.run_ids.length === 1 ? '' : 's'} recorded — view in
            History for full transcripts.
          </p>
        )}
      </CardBody>
    </Card>
  )
}
