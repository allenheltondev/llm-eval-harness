/**
 * One suite, several models, side by side.
 *
 * Reads `GET /evaluations/compare` — a pure function of the stored results, so
 * opening it costs nothing — and shows three things: who won (or that it is a
 * tie: level quality is never called a win), how each model scored and what it
 * cost, and case by case where the models disagree, which are the ones worth
 * reading.
 *
 * A model is ranked only when its evaluation completed; one that errored or
 * was cancelled is listed with its status instead, because its partial numbers
 * are not a fair basis for comparison.
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
import type { CaseAgreement, ComparisonArm, ComparisonCaseRow, ModelComparison } from '../../api'
import { COST_ESTIMATE_NOTE, formatUsd } from '../../components/cost'
import { statusTone } from '../../components/status'
import { evaluationHref } from '../../routing'

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

function percent(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`
}

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

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm" data-testid="compare-arms">
        <caption className="sr-only">Models ranked by pass rate, then score, then cost</caption>
        <thead>
          <tr className="text-left text-xs text-muted-foreground border-b border-border">
            <th scope="col" className="py-2 pr-3">
              Rank
            </th>
            <th scope="col" className="py-2 pr-3">
              Model
            </th>
            <th scope="col" className="py-2 pr-3">
              Cases passed
            </th>
            <th scope="col" className="py-2 pr-3">
              Score
            </th>
            <th scope="col" className="py-2 pr-3" title={COST_ESTIMATE_NOTE}>
              Est. cost
            </th>
            <th scope="col" className="py-2 pr-3">
              Evaluation
            </th>
          </tr>
        </thead>
        <tbody>
          {[...ranked, ...unranked].map(arm => {
            const rank = comparison.ranking.indexOf(arm.label)
            return (
              <tr
                key={arm.label}
                className="border-b border-border last:border-0"
                data-testid={`compare-arm-${arm.label}`}
              >
                <td className="py-2 pr-3">{rank === -1 ? '—' : rank + 1}</td>
                <td className="py-2 pr-3 font-medium whitespace-nowrap">
                  {arm.label}
                  {comparison.winner === arm.label && (
                    <span className="ml-2 text-xs font-semibold text-success-700">Winner</span>
                  )}
                  {comparison.tied.includes(arm.label) && (
                    <span className="ml-2 text-xs font-semibold text-muted-foreground">Tied</span>
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
                <td className="py-2 pr-3">{formatUsd(arm.cost_usd)}</td>
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
    <div className="overflow-x-auto">
      <table className="w-full text-sm" data-testid="compare-cases">
        <caption className="sr-only">Each case's result for every model</caption>
        <thead>
          <tr className="text-left text-xs text-muted-foreground border-b border-border">
            <th scope="col" className="py-2 pr-3">
              Case
            </th>
            {labels.map(label => (
              <th key={label} scope="col" className="py-2 pr-3 whitespace-nowrap">
                {label}
              </th>
            ))}
            <th scope="col" className="py-2 pr-3">
              Result
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => (
            <tr
              key={row.id}
              className={`border-b border-border last:border-0 ${
                row.agreement === 'split' ? 'bg-warning-50' : ''
              }`}
              data-testid={`compare-case-${row.id}`}
            >
              <th scope="row" className="py-2 pr-3 text-left font-medium whitespace-nowrap">
                {row.id}
              </th>
              {labels.map(label => (
                <td key={label} className="py-2 pr-3">
                  <CaseCell result={row.results[label] ?? null} />
                </td>
              ))}
              <td className="py-2 pr-3 text-xs text-muted-foreground">
                {AGREEMENT_LABELS[row.agreement]}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ModelComparisonView({ evaluationIds, onClose }: ModelComparisonViewProps) {
  const [comparison, setComparison] = useState<ModelComparison | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [onlySplit, setOnlySplit] = useState(false)
  const key = evaluationIds.join(',')

  useEffect(() => {
    const controller = new AbortController()
    setComparison(null)
    setError(null)
    api.evaluations
      .compare(evaluationIds, { signal: controller.signal })
      .then(setComparison)
      .catch((failure: unknown) => {
        if (controller.signal.aborted) return
        setError(failure instanceof Error ? failure.message : String(failure))
      })
    return () => controller.abort()
    // `key` stands for the ids: a new array with the same ids is not a new comparison.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const summary = comparison ? verdict(comparison) : null

  return (
    <Card role="region" aria-labelledby="model-compare-heading" data-testid="model-compare">
      <CardHeader className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="model-compare-heading" className="card-title">
          Compare models
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
            {isNarrow(comparison) && (
              <p className="text-xs text-muted-foreground" data-testid="model-compare-narrow">
                The top models are within one case of each other. On a suite this size that gap can
                be one unlucky answer: add cases or raise <code>repeats</code> before relying on it.
              </p>
            )}

            <ArmTable comparison={comparison} />

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
