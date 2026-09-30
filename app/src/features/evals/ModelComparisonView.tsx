/**
 * One suite, several arms (a model and the prompt it was given), side by side.
 *
 * Reads `GET /evaluations/compare` — a pure function of the stored results, so
 * opening it costs nothing. It answers two different questions:
 *
 * - *Which is best?* The ranking: who won (or that it is a tie: level quality
 *   is never called a win), what each scored and cost, and case by case where
 *   the arms disagree.
 * - *Can this stand in for that?* Pick a baseline and every other arm is
 *   measured against it: the cases it broke, whether a suite this size can say
 *   it is ready, and what it changes. See `ReadinessPanel`.
 *
 * An arm is ranked only when its evaluation completed; one that errored or was
 * cancelled is listed with its status instead, because its partial numbers are
 * not a fair basis for comparison.
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  SkeletonLoader,
  StatusBadge
} from '@readysetcloud/ui'
import { api } from '../../api'
import type {
  CaseAgreement,
  ComparisonArm,
  ComparisonCaseRow,
  ModelComparison,
  ReadinessStatus
} from '../../api'
import { COST_ESTIMATE_NOTE, formatUsd } from '../../components/cost'
import { statusTone } from '../../components/status'
import { evaluationHref } from '../../routing'
import { EffectsPanel, WarningsList } from './ComparisonInsights'
import { duration, percent } from './comparisonFormat'
import ReadinessPanel from './ReadinessPanel'

export interface ModelComparisonViewProps {
  /** The suite evaluations to compare, one per model. */
  evaluationIds: string[]
  onClose?: () => void
}

const CASE_LABELS: Record<string, string> = {
  passed: 'Pass',
  failed: 'Fail',
  error: 'Error',
  judge_error: 'Not judged'
}

const AGREEMENT_LABELS: Record<CaseAgreement, string> = {
  all_passed: 'All passed',
  all_failed: 'All failed',
  split: 'Models disagree',
  incomplete: 'Incomplete'
}

const READINESS_LABELS: Record<ReadinessStatus, string> = {
  ready: 'Ready',
  not_ready: 'Not ready',
  inconclusive: 'Inconclusive'
}

const READINESS_TONES = { ready: 'success', not_ready: 'error', inconclusive: 'warning' } as const

function passedOf(arm: ComparisonArm): string {
  if (arm.cases_passed === null || arm.cases_total === null) return '—'
  return `${arm.cases_passed}/${arm.cases_total} (${percent(arm.pass_rate)})`
}

/**
 * Whether the top two ranked models are within one case of each other — a gap
 * that, on a suite this size, one flaky answer can produce.
 */
function isNarrow(comparison: ModelComparison): boolean {
  const [first, second] = comparison.ranking
    .map(label => comparison.arms.find(arm => arm.label === label))
    .filter((arm): arm is ComparisonArm => arm !== undefined)
  if (!first || !second) return false
  return Math.abs((first.cases_passed ?? 0) - (second.cases_passed ?? 0)) <= 1
}

function verdict(comparison: ModelComparison): {
  tone: 'success' | 'info' | 'error'
  text: string
} {
  if (comparison.winner) {
    return { tone: 'success', text: `${comparison.winner} did best.` }
  }
  if (comparison.tied.length > 1) {
    return {
      tone: 'info',
      text: `A tie: ${comparison.tied.join(' and ')} are level on pass rate and score.`
    }
  }
  return { tone: 'error', text: 'No model completed, so there is nothing to rank.' }
}

function ArmTable({ comparison }: { comparison: ModelComparison }) {
  const byLabel = new Map(comparison.arms.map(arm => [arm.label, arm]))
  const ranked = comparison.ranking.flatMap(label => byLabel.get(label) ?? [])
  const unranked = comparison.arms.filter(arm => !comparison.ranking.includes(arm.label))
  const showLatency = comparison.arms.some(arm => arm.latency_p95_ms !== null)
  const showVerdict = comparison.baseline !== null

  return (
    // `relative` makes this the containing block for the visually hidden text in the cells:
    // an absolutely positioned element escapes an overflow clip otherwise, and a note in a
    // far-right column would widen the whole page.
    <div className="relative overflow-x-auto">
      <table className="w-full text-sm" data-testid="compare-arms">
        <caption className="sr-only">Arms ranked by pass rate, then score, then cost</caption>
        <thead>
          <tr className="text-left text-xs text-muted-foreground border-b border-border">
            <th scope="col" className="py-2 pr-3">
              Rank
            </th>
            <th scope="col" className="py-2 pr-3">
              Arm
            </th>
            <th scope="col" className="py-2 pr-3">
              Cases passed
            </th>
            <th scope="col" className="py-2 pr-3">
              Score
            </th>
            {showLatency && (
              <th scope="col" className="py-2 pr-3">
                p95 latency
              </th>
            )}
            <th scope="col" className="py-2 pr-3" title={COST_ESTIMATE_NOTE}>
              Est. cost
            </th>
            {showVerdict && (
              <th scope="col" className="py-2 pr-3">
                As a fallback
              </th>
            )}
            <th scope="col" className="py-2 pr-3">
              Evaluation
            </th>
          </tr>
        </thead>
        <tbody>
          {[...ranked, ...unranked].map(arm => {
            const rank = comparison.ranking.indexOf(arm.label)
            const isBaseline = comparison.baseline === arm.label
            return (
              <tr
                key={arm.label}
                className="border-b border-border last:border-0"
                data-testid={`compare-arm-${arm.label}`}
              >
                <td className="py-2 pr-3">{rank === -1 ? '—' : rank + 1}</td>
                <td className="py-2 pr-3 font-medium whitespace-nowrap">
                  {arm.label}
                  {isBaseline && (
                    <span className="ml-2 text-xs font-semibold text-primary-700">Baseline</span>
                  )}
                  {comparison.winner === arm.label && (
                    <span className="ml-2 text-xs font-semibold text-success-700">Winner</span>
                  )}
                  {comparison.tied.includes(arm.label) && (
                    <span className="ml-2 text-xs font-semibold text-muted-foreground">Tied</span>
                  )}
                  {arm.name && (
                    <span className="block text-xs font-normal text-muted-foreground">
                      {arm.provider}:{arm.model_id}
                      {arm.prompt_id && ` · prompt ${arm.prompt_id}`}
                    </span>
                  )}
                </td>
                <td className="py-2 pr-3">
                  {rank === -1 ? (
                    <span className="text-muted-foreground">
                      Not ranked ({arm.status})
                      {arm.pass_rate !== null && ` · ${passedOf(arm)} so far`}
                    </span>
                  ) : (
                    passedOf(arm)
                  )}
                  {arm.budget_exhausted && (
                    <span className="block text-xs text-muted-foreground">
                      Stopped by its budget: some cases never ran
                    </span>
                  )}
                </td>
                <td className="py-2 pr-3 whitespace-nowrap">
                  {/* A partial run's score is not comparable, so it is not shown beside the ranked. */}
                  {rank === -1 || arm.score === null
                    ? '—'
                    : `${arm.score}${arm.grade ? ` ${arm.grade}` : ''}`}
                </td>
                {showLatency && (
                  <td className="py-2 pr-3 whitespace-nowrap">{duration(arm.latency_p95_ms)}</td>
                )}
                <td className="py-2 pr-3">{formatUsd(arm.cost_usd)}</td>
                {showVerdict && (
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {isBaseline || !arm.vs_baseline ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <StatusBadge tone={READINESS_TONES[arm.vs_baseline.status]} role={undefined}>
                        {READINESS_LABELS[arm.vs_baseline.status]}
                      </StatusBadge>
                    )}
                  </td>
                )}
                <td className="py-2 pr-3">
                  {arm.evaluation_id ? (
                    <a
                      className="text-primary-700 underline"
                      href={evaluationHref(arm.evaluation_id)}
                    >
                      Open
                    </a>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Choose the arm the others are measured against; the choice is a new comparison. */
function BaselinePicker({
  comparison,
  onChange
}: {
  comparison: ModelComparison
  onChange: (evaluationId: string) => void
}) {
  const selectable = comparison.arms.filter(arm => arm.evaluation_id !== null)
  const current = selectable.find(arm => arm.label === comparison.baseline)?.evaluation_id ?? ''
  return (
    <label className="flex flex-wrap items-center gap-2 text-sm">
      <span className="font-medium">Measure against</span>
      <select
        className="rounded border border-border bg-background px-2 py-1 text-sm"
        data-testid="model-compare-baseline"
        value={current}
        onChange={event => {
          if (event.target.value) onChange(event.target.value)
        }}
      >
        {current === '' && <option value="">Choose a baseline…</option>}
        {selectable.map(arm => (
          <option key={arm.label} value={arm.evaluation_id ?? ''}>
            {arm.label}
          </option>
        ))}
      </select>
      {comparison.baseline === null && (
        <span className="text-xs text-muted-foreground">
          to see what each other arm breaks and whether it could stand in
        </span>
      )}
    </label>
  )
}

function CaseCell({ result }: { result: ComparisonCaseRow['results'][string] }) {
  if (result === null) return <span className="text-muted-foreground">Not run</span>
  return (
    <span className="inline-flex items-center gap-2">
      <StatusBadge tone={statusTone(result.status)} role={undefined}>
        {CASE_LABELS[result.status] ?? result.status}
      </StatusBadge>
      {result.score !== null && (
        <span className="text-xs text-muted-foreground">{result.score.toFixed(2)}</span>
      )}
    </span>
  )
}

function CaseGrid({ comparison, onlySplit }: { comparison: ModelComparison; onlySplit: boolean }) {
  const labels = [
    ...comparison.ranking,
    ...comparison.arms.map(arm => arm.label).filter(label => !comparison.ranking.includes(label))
  ]
  const rows = onlySplit
    ? comparison.cases.filter(row => row.agreement === 'split')
    : comparison.cases

  if (rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="compare-no-disagreements">
        The models agree on every case.
      </p>
    )
  }

  return (
    // `relative` makes this the containing block for the visually hidden text in the cells:
    // an absolutely positioned element escapes an overflow clip otherwise, and a note in a
    // far-right column would widen the whole page.
    <div className="relative overflow-x-auto">
      <table className="w-full text-sm" data-testid="compare-cases">
        <caption className="sr-only">Each case's result for every arm</caption>
        <thead>
          <tr className="text-left text-xs text-muted-foreground border-b border-border">
            <th scope="col" className="py-2 pr-3">
              Case
            </th>
            {labels.map(label => (
              <th key={label} scope="col" className="py-2 pr-3 whitespace-nowrap">
                {label}
                {label === comparison.baseline && ' (baseline)'}
              </th>
            ))}
            <th scope="col" className="py-2 pr-3">
              Result
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => {
            const baselineResult = comparison.baseline ? row.results[comparison.baseline] : null
            const baselinePassed = baselineResult?.status === 'passed'
            return (
              <tr
                key={row.id}
                className={`border-b border-border last:border-0 ${
                  row.agreement === 'split' ? 'bg-warning-50' : ''
                }`}
                data-testid={`compare-case-${row.id}`}
              >
                <th scope="row" className="py-2 pr-3 text-left font-medium whitespace-nowrap">
                  {row.id}
                  {row.critical && (
                    <span
                      className="ml-2 text-xs font-semibold text-error-700"
                      data-testid={`compare-critical-${row.id}`}
                    >
                      critical
                    </span>
                  )}
                </th>
                {labels.map(label => {
                  const result = row.results[label] ?? null
                  // A cell where the baseline passes and this arm does not: a regression.
                  const regressed =
                    baselinePassed && label !== comparison.baseline && result?.status !== 'passed'
                  return (
                    <td
                      key={label}
                      className={`py-2 pr-3 ${regressed ? 'bg-error-50' : ''}`}
                      data-regression={regressed ? 'true' : undefined}
                    >
                      <CaseCell result={result} />
                      {regressed && <span className="sr-only"> (regressed from the baseline)</span>}
                    </td>
                  )
                })}
                <td className="py-2 pr-3 text-xs text-muted-foreground">
                  {AGREEMENT_LABELS[row.agreement]}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function ModelComparisonView({ evaluationIds, onClose }: ModelComparisonViewProps) {
  const [comparison, setComparison] = useState<ModelComparison | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [onlySplit, setOnlySplit] = useState(false)
  // The baseline the reader picked (an evaluation id); until then, the server's choice.
  const [baseline, setBaseline] = useState<string | undefined>(undefined)
  const key = evaluationIds.join(',')

  useEffect(() => {
    setBaseline(undefined)
  }, [key])

  useEffect(() => {
    const controller = new AbortController()
    setComparison(null)
    setError(null)
    api.evaluations
      .compare(evaluationIds, { signal: controller.signal, baseline })
      .then(setComparison)
      .catch((failure: unknown) => {
        if (controller.signal.aborted) return
        setError(failure instanceof Error ? failure.message : String(failure))
      })
    return () => controller.abort()
    // `key` stands for the ids: a new array with the same ids is not a new comparison.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, baseline])

  const summary = comparison ? verdict(comparison) : null

  return (
    <Card role="region" aria-labelledby="model-compare-heading" data-testid="model-compare">
      <CardHeader className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="model-compare-heading" className="card-title">
          Compare models and prompts
          {comparison?.suite.name && (
            <span className="ml-2 text-sm font-normal text-muted-foreground">
              {comparison.suite.name} · {comparison.suite.cases} cases
            </span>
          )}
        </h2>
        {onClose && (
          <Button variant="ghost" size="sm" onClick={onClose} data-testid="model-compare-close">
            Close comparison
          </Button>
        )}
      </CardHeader>

      <CardBody className="space-y-5">
        {error === null && comparison === null && (
          <div role="status" data-testid="model-compare-loading">
            <span className="sr-only">Comparing…</span>
            <SkeletonLoader count={3} />
          </div>
        )}

        {error !== null && (
          <Alert variant="error" data-testid="model-compare-error">
            Could not compare these evaluations: {error}
          </Alert>
        )}

        {comparison && summary && (
          <>
            <Alert variant={summary.tone} data-testid="model-compare-verdict">
              {summary.text}
            </Alert>
            <WarningsList warnings={comparison.warnings} />
            {isNarrow(comparison) && (
              <p className="text-xs text-muted-foreground" data-testid="model-compare-narrow">
                The top models are within one case of each other. On a suite this size that gap can
                be one unlucky answer: add cases or raise <code>repeats</code> before relying on it.
              </p>
            )}

            <BaselinePicker comparison={comparison} onChange={setBaseline} />
            <ArmTable comparison={comparison} />
            <ReadinessPanel comparison={comparison} />
            {comparison.effects && <EffectsPanel effects={comparison.effects} />}

            <section aria-labelledby="model-compare-cases-heading" className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 id="model-compare-cases-heading" className="text-sm font-semibold">
                  Case by case
                </h3>
                <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    data-testid="model-compare-split-only"
                    className="h-4 w-4 rounded border-border accent-primary-600"
                    checked={onlySplit}
                    onChange={event => setOnlySplit(event.target.checked)}
                  />
                  Only where the models disagree ({comparison.split_cases.length})
                </label>
              </div>
              <CaseGrid comparison={comparison} onlySplit={onlySplit} />
            </section>
          </>
        )}
      </CardBody>
    </Card>
  )
}
