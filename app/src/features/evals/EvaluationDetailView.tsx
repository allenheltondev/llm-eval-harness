/**
 * Everything recorded about one evaluation, below its headline result: where
 * it came from, what exactly was run, each suite case's verdict, and a link to
 * every run it made.
 *
 * `EvalResultView` stays the compact grade/score/metrics card; this is the
 * full-width page body for the same evaluation, so a suite's case table has
 * room to be read. Everything here comes from the `EvaluationDetail` row
 * (`config` is the request as it was stored), so an evaluation started from
 * the CLI shows exactly as one started here.
 */

import { useState } from 'react'
import type {
  EvaluationDetail,
  EvaluationResult,
  EvaluationStoredConfig,
  RunRequest,
  StoredSuite,
  SuiteCaseResult
} from '../../api'
import { evaluationHref, runHref } from '../../routing'

const SOURCE_LABELS: Record<string, string> = { cli: 'CLI', ui: 'Web UI', api: 'API' }

export function sourceLabel(source: string | null | undefined): string | null {
  if (!source) return null
  return SOURCE_LABELS[source] ?? source
}

const CASE_STATUS: Record<string, { label: string; className: string }> = {
  passed: { label: 'Pass', className: 'bg-green-100 text-green-800' },
  failed: { label: 'Fail', className: 'bg-red-100 text-red-800' },
  error: { label: 'Error', className: 'bg-amber-100 text-amber-800' },
  judge_error: { label: 'Not judged', className: 'bg-amber-100 text-amber-800' }
}

function caseStatus(status: string) {
  return CASE_STATUS[status] ?? { label: status, className: 'bg-gray-100 text-gray-700' }
}

function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}

function errorText(error: Record<string, unknown> | null): string | null {
  if (!error) return null
  if (typeof error.message === 'string' && error.message !== '') return error.message
  if (typeof error.code === 'string' && error.code !== '') return error.code
  return 'unknown error'
}

/** The absolute URL of this evaluation's page, for sharing. */
function shareableLink(evaluationId: string): string {
  const { origin, pathname } = window.location
  return `${origin}${pathname}${evaluationHref(evaluationId)}`
}

function CopyLinkButton({ evaluationId }: { evaluationId: string }) {
  const [copied, setCopied] = useState(false)
  if (!navigator.clipboard) return null
  return (
    <button
      type="button"
      className="btn-secondary py-1 px-2 text-xs"
      data-testid="eval-copy-link"
      onClick={() => {
        void navigator.clipboard.writeText(shareableLink(evaluationId)).then(() => setCopied(true))
      }}
    >
      {copied ? 'Link copied' : 'Copy link'}
    </button>
  )
}

/** One numbered run: a link when its run was recorded, red when it failed. */
interface RunSlot {
  label: string
  runId: string | null
  failed: boolean
  /** Shown when there is no run to open. */
  missing: string
}

function RunSlots({ slots }: { slots: RunSlot[] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {slots.map((slot, index) => {
        const className = `px-1.5 py-0.5 rounded text-xs ${
          slot.failed ? 'bg-red-50 text-red-700' : 'bg-gray-100 text-primary-700'
        }`
        if (!slot.runId) {
          return (
            <span key={index} className={`${className} opacity-60`} title={slot.missing}>
              {slot.label}
            </span>
          )
        }
        return (
          <a
            key={slot.runId}
            href={runHref(slot.runId)}
            className={`${className} hover:bg-primary-50`}
            title={`${slot.failed ? 'Failed run' : 'Run'} ${slot.runId} — open in History`}
          >
            {slot.label}
          </a>
        )
      })}
    </span>
  )
}

function successfulSlots(runIds: string[], label: (index: number) => string): RunSlot[] {
  return runIds.map((runId, index) => ({
    label: label(index),
    runId,
    failed: false,
    missing: ''
  }))
}

/**
 * A case's runs, one per repeat and labelled by repeat: a failed repeat keeps
 * its number (and its link, when its run was recorded) rather than the
 * successful ones being renumbered around it. Results stored before `repeats`
 * existed only know their successful runs, so those are listed in order.
 */
function repeatSlots(result: SuiteCaseResult): RunSlot[] {
  if (!result.repeats) return successfulSlots(result.run_ids, index => `#${index + 1}`)
  return result.repeats.map((repeat, index) => ({
    label: `#${index + 1}`,
    runId: repeat.run_id,
    failed: !repeat.ran,
    missing: `Repeat ${index + 1} failed before a run was recorded`
  }))
}

/**
 * Every run of a non-suite evaluation, numbered by its place in the batch.
 * `run_ids` holds the successful runs in order and each `failed_runs` entry
 * names its own place, so the failures go back where they ran and the
 * successes fill the rest. A failure keeps its link when its run was recorded
 * (older results never stored one).
 */
export function batchSlots(
  runIds: string[],
  failedRuns: EvaluationResult['failed_runs']
): RunSlot[] {
  const total = runIds.length + failedRuns.length
  const failedAt = new Map(failedRuns.map(failure => [failure.index, failure]))
  const placeable =
    failedAt.size === failedRuns.length &&
    failedRuns.every(failure => failure.index >= 0 && failure.index < total)
  const failedSlot = (index: number, runId: string | null | undefined): RunSlot => ({
    label: `Run ${index + 1}`,
    runId: runId ?? null,
    failed: true,
    missing: `Run ${index + 1} failed before it was recorded`
  })
  if (!placeable) {
    return [
      ...successfulSlots(runIds, index => `Run ${index + 1}`),
      ...failedRuns.map(failure => failedSlot(failure.index, failure.run_id))
    ]
  }
  const successes = runIds[Symbol.iterator]()
  return Array.from({ length: total }, (_, index) => {
    const failure = failedAt.get(index)
    if (failure) return failedSlot(index, failure.run_id)
    return {
      label: `Run ${index + 1}`,
      runId: successes.next().value ?? null,
      failed: false,
      missing: ''
    }
  })
}

function SuiteCases({ cases, suite }: { cases: SuiteCaseResult[]; suite?: StoredSuite }) {
  const definitions = new Map((suite?.cases ?? []).map(definition => [definition.id, definition]))
  return (
    <section aria-labelledby="eval-cases-heading" data-testid="eval-cases">
      <h3 id="eval-cases-heading" className="text-sm font-semibold text-gray-900 mb-2">
        Cases
      </h3>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
              <th className="py-2 pr-3">Case</th>
              <th className="py-2 pr-3">Result</th>
              <th className="py-2 pr-3">Score</th>
              <th className="py-2 pr-3">Repeats</th>
              <th className="py-2 pr-3">Judge</th>
              <th className="py-2">Runs</th>
            </tr>
          </thead>
          <tbody>
            {cases.map(result => {
              const status = caseStatus(result.status)
              const definition = definitions.get(result.id)
              const problem = errorText(result.error)
              return (
                <tr
                  key={result.id}
                  className="border-b border-gray-100 align-top"
                  data-testid={`eval-case-${result.id}`}
                >
                  <td className="py-2 pr-3">
                    <span className="font-mono text-xs text-gray-900">{result.id}</span>
                    {definition && (
                      <details className="mt-1 text-xs text-gray-600">
                        <summary className="cursor-pointer text-gray-500">Case</summary>
                        <dl className="mt-1 space-y-1 max-w-md">
                          <dt className="font-medium">Input</dt>
                          <dd className="whitespace-pre-wrap">{definition.input}</dd>
                          {definition.expected && (
                            <>
                              <dt className="font-medium">Expected</dt>
                              <dd className="whitespace-pre-wrap">{definition.expected}</dd>
                            </>
                          )}
                          {definition.criteria && (
                            <>
                              <dt className="font-medium">Criteria</dt>
                              <dd className="whitespace-pre-wrap">{definition.criteria}</dd>
                            </>
                          )}
                        </dl>
                      </details>
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    <span
                      className={`px-2 py-0.5 rounded-full text-xs font-medium ${status.className}`}
                    >
                      {status.label}
                    </span>
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs">{formatScore(result.score)}</td>
                  <td className="py-2 pr-3 font-mono text-xs text-gray-600">
                    {result.scores.map(formatScore).join(' · ') || '—'}
                  </td>
                  <td className="py-2 pr-3 text-xs text-gray-700 max-w-md">
                    {problem && <p className="text-red-700">{problem}</p>}
                    {result.reasoning && <p className="whitespace-pre-wrap">{result.reasoning}</p>}
                  </td>
                  <td className="py-2">
                    <RunSlots slots={repeatSlots(result)} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function ConfigRow({ label, value }: { label: string; value: string | null | undefined }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <div className="min-w-0">
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className="text-sm text-gray-900 whitespace-pre-wrap break-words">{value}</dd>
    </div>
  )
}

function inferenceSummary(run: RunRequest): string | null {
  const inference = run.inference
  if (!inference) return null
  const parts = Object.entries(inference)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${String(value)}`)
  return parts.length ? parts.join(', ') : null
}

function Configuration({ config }: { config: EvaluationStoredConfig }) {
  const run = config.run_config
  const suite = config.suite
  return (
    <section aria-labelledby="eval-config-heading" data-testid="eval-config">
      <h3 id="eval-config-heading" className="text-sm font-semibold text-gray-900 mb-2">
        Configuration
      </h3>
      <dl className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {run && (
          <>
            <ConfigRow
              label="Model"
              value={`${run.model_id}${run.provider ? ` (${run.provider})` : ''}`}
            />
            <ConfigRow label="Toolset" value={run.toolset ?? null} />
            <ConfigRow label="Inference" value={inferenceSummary(run)} />
            <ConfigRow
              label="Guardrail"
              value={run.guardrail ? `${run.guardrail.id} (${run.guardrail.version})` : null}
            />
            <ConfigRow label="System prompt" value={run.system_prompt ?? null} />
            {!suite && <ConfigRow label="User prompt" value={run.user_prompt} />}
          </>
        )}
        {suite ? (
          <ConfigRow
            label="Suite"
            value={`${suite.name ?? 'Unnamed'} — ${suite.cases.length} cases × ${suite.repeats} repeats, passing at ${suite.pass_threshold}`}
          />
        ) : (
          <ConfigRow label="Runs" value={String(config.n)} />
        )}
        <ConfigRow
          label="Judge"
          value={`${config.grader.model_id}${config.grader.provider ? ` (${config.grader.provider})` : ''}`}
        />
        <ConfigRow label="Judge system prompt" value={config.grader.system_prompt} />
        <ConfigRow label="Rubric" value={config.rubric} />
      </dl>
    </section>
  )
}

interface EvaluationDetailViewProps {
  evaluation: EvaluationDetail
  /** The result to show — the live one while following, else the row's. */
  result: EvaluationResult | null
}

export default function EvaluationDetailView({ evaluation, result }: EvaluationDetailViewProps) {
  const source = sourceLabel(evaluation.source ?? evaluation.config.source)
  const runSlots = result
    ? batchSlots(result.run_ids, result.failed_runs)
    : successfulSlots(evaluation.run_ids, index => `Run ${index + 1}`)
  return (
    <section
      className="card space-y-5"
      aria-labelledby="eval-detail-heading"
      data-testid="eval-detail"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <h2 id="eval-detail-heading" className="text-base font-semibold text-gray-900">
            {evaluation.config.suite?.name ?? `${evaluation.kind} evaluation`}
          </h2>
          {source && (
            <span
              className="px-2 py-0.5 rounded-full text-xs font-medium bg-violet-100 text-violet-800"
              data-testid="eval-detail-source"
            >
              {source}
            </span>
          )}
          <span className="font-mono text-xs text-gray-500">{evaluation.id}</span>
        </div>
        <CopyLinkButton evaluationId={evaluation.id} />
      </div>

      {result?.truncated && (
        <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-2">
          Some of the judge&apos;s reasoning was shortened to fit the stored result.
        </p>
      )}

      {result?.cases && result.cases.length > 0 && (
        <SuiteCases cases={result.cases} suite={evaluation.config.suite} />
      )}

      <Configuration config={evaluation.config} />

      {!result?.cases && runSlots.length > 0 && (
        <section aria-labelledby="eval-runs-heading" data-testid="eval-runs">
          <h3 id="eval-runs-heading" className="text-sm font-semibold text-gray-900 mb-2">
            Runs
          </h3>
          <RunSlots slots={runSlots} />
        </section>
      )}
    </section>
  )
}
