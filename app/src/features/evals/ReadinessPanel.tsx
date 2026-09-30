/**
 * Can each candidate stand in for the baseline?
 *
 * The question a ranking cannot answer. For every arm other than the baseline:
 * the verdict (ready, not ready, or inconclusive), the reasons, the cases it
 * broke and fixed, what it changes, and how it compares on latency and cost.
 *
 * "Inconclusive" is the honest middle. Zero regressions in ten cases is
 * consistent with a true regression rate of about a quarter, so a small suite
 * is never allowed to certify a fallback; the reason says how many cases would.
 */

import { Badge, Card, CardBody, CardHeader, StatusBadge } from '@readysetcloud/ui'
import type { StatusBadgeTone } from '@readysetcloud/ui'
import type {
  BaselineAssessment,
  ComparisonArm,
  ComparisonBar,
  ModelComparison,
  ReadinessStatus
} from '../../api'
import { duration, percent, points, probability, ratio, scoreDelta } from './comparisonFormat'

const STATUS_LABELS: Record<ReadinessStatus, string> = {
  ready: 'Ready',
  not_ready: 'Not ready',
  inconclusive: 'Inconclusive'
}

const STATUS_TONES: Record<ReadinessStatus, StatusBadgeTone> = {
  ready: 'success',
  not_ready: 'error',
  inconclusive: 'warning'
}

const BAR_SOURCES: Record<ComparisonBar['source'], string> = {
  suite: "the suite's bar",
  default: 'the default bar: the suite sets none',
  requested: 'the requested bar'
}

function barSummary(bar: ComparisonBar): string {
  const parts = [
    `at most ${percent(bar.max_regression_rate)} of the cases the baseline passes may regress`
  ]
  if (bar.max_latency_ratio !== null) parts.push(`p95 latency within ${bar.max_latency_ratio}×`)
  if (bar.max_cost_ratio !== null) parts.push(`cost within ${bar.max_cost_ratio}×`)
  return parts.join('; ')
}

/** How much a difference from the baseline can be trusted as more than chance. */
function chanceNote(assessment: BaselineAssessment): string | null {
  const differing = assessment.regressions.length + assessment.improvements.length
  if (differing === 0) return null
  const p = probability(assessment.sign_test_p)
  return assessment.sign_test_p < 0.05
    ? `It differs from the baseline on ${differing} case${differing === 1 ? '' : 's'}, which is unlikely to be chance (sign test p=${p}).`
    : `It differs from the baseline on ${differing} case${differing === 1 ? '' : 's'}, which a sign test cannot tell from chance (p=${p}).`
}

function CaseChips({
  ids,
  critical,
  tone
}: {
  ids: string[]
  critical: Set<string>
  tone: 'broke' | 'fixed'
}) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {ids.map(id => (
        <Badge
          key={id}
          variant={tone === 'broke' ? 'neutral' : 'primary'}
          data-testid={`readiness-case-${tone}-${id}`}
        >
          {id}
          {critical.has(id) && <span className="ml-1 font-semibold">critical</span>}
        </Badge>
      ))}
    </span>
  )
}

function Candidate({
  arm,
  baseline,
  critical
}: {
  arm: ComparisonArm
  baseline: ComparisonArm
  critical: Set<string>
}) {
  const assessment = arm.vs_baseline
  if (!assessment) {
    return (
      <article
        className="border border-border rounded-lg p-3 space-y-1"
        data-testid={`readiness-${arm.label}`}
      >
        <h4 className="text-sm font-semibold break-words">{arm.label}</h4>
        <p className="text-sm text-muted-foreground">
          Cannot be measured: {baseline.label}'s evaluation did not complete.
        </p>
      </article>
    )
  }
  const chance = chanceNote(assessment)
  const stats: Array<[string, string]> = [
    ['Pass rate', points(assessment.pass_rate_delta)],
    ['Score', scoreDelta(assessment.score_delta)],
    ['p95 latency', ratio(assessment.latency_ratio)],
    ['Est. cost', ratio(assessment.cost_ratio)]
  ]

  return (
    <article
      className="border border-border rounded-lg p-3 space-y-3"
      data-testid={`readiness-${arm.label}`}
    >
      <header className="flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold break-words">{arm.label}</h4>
        <StatusBadge
          tone={STATUS_TONES[assessment.status]}
          role={undefined}
          data-testid={`readiness-status-${arm.label}`}
        >
          {STATUS_LABELS[assessment.status]}
        </StatusBadge>
        {assessment.changes.length > 0 ? (
          <span
            className="text-xs text-muted-foreground"
            data-testid={`readiness-changes-${arm.label}`}
          >
            differs from the baseline in {assessment.changes.join(' and ')}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">same settings as the baseline</span>
        )}
      </header>

      <ul
        className="list-disc pl-5 text-sm space-y-1"
        data-testid={`readiness-reasons-${arm.label}`}
      >
        {assessment.reasons.map(reason => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>

      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm">
        {stats.map(([name, value]) => (
          <div key={name}>
            <dt className="text-xs text-muted-foreground">{name} vs baseline</dt>
            <dd className="font-medium">{value}</dd>
          </div>
        ))}
      </dl>

      {assessment.regression_rate !== null && (
        <p className="text-xs text-muted-foreground" data-testid={`readiness-bound-${arm.label}`}>
          {assessment.regressions.length} of {assessment.baseline_passed} baseline-passing cases
          regressed ({percent(assessment.regression_rate)}); at 95% confidence the true rate is at
          most {percent(assessment.regression_upper_bound)}.
        </p>
      )}

      {assessment.regressions.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium">Broke</p>
          <CaseChips ids={assessment.regressions} critical={critical} tone="broke" />
        </div>
      )}
      {assessment.improvements.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium">Fixed</p>
          <CaseChips ids={assessment.improvements} critical={critical} tone="fixed" />
        </div>
      )}
      {chance && <p className="text-xs text-muted-foreground">{chance}</p>}
    </article>
  )
}

export default function ReadinessPanel({ comparison }: { comparison: ModelComparison }) {
  const baseline = comparison.arms.find(arm => arm.label === comparison.baseline)
  if (!baseline) return null
  const candidates = comparison.arms.filter(arm => arm.label !== comparison.baseline)
  const critical = new Set(comparison.cases.filter(row => row.critical).map(row => row.id))

  return (
    <Card role="region" aria-labelledby="readiness-heading" data-testid="readiness-panel">
      <CardHeader>
        <h3 id="readiness-heading" className="text-sm font-semibold">
          Can these stand in for {baseline.label}?
        </h3>
      </CardHeader>
      <CardBody className="space-y-3">
        {comparison.bar && (
          <p className="text-xs text-muted-foreground" data-testid="readiness-bar">
            Judged by {BAR_SOURCES[comparison.bar.source]}: {barSummary(comparison.bar)}. Baseline
            p95 latency {duration(baseline.latency_p95_ms)}.
          </p>
        )}
        {candidates.map(arm => (
          <Candidate key={arm.label} arm={arm} baseline={baseline} critical={critical} />
        ))}
      </CardBody>
    </Card>
  )
}
