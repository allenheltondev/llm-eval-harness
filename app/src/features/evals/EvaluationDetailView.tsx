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

function RunLinks({ runIds, label }: { runIds: string[]; label: (index: number) => string }) {
  return (
    <span className="flex flex-wrap gap-1">
      {runIds.map((runId, index) => (
        <a
          key={runId}
          href={runHref(runId)}
          className="px-1.5 py-0.5 rounded bg-gray-100 text-xs text-primary-700 hover:bg-primary-50"
          title={`Open run ${runId} in History`}
        >
          {label(index)}
        </a>
      ))}
    </span>
  )
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
                    <RunLinks runIds={result.run_ids} label={index => `#${index + 1}`} />
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
  const runIds = result?.run_ids ?? evaluation.run_ids
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

      {!result?.cases && runIds.length > 0 && (
        <section aria-labelledby="eval-runs-heading" data-testid="eval-runs">
          <h3 id="eval-runs-heading" className="text-sm font-semibold text-gray-900 mb-2">
            Runs
          </h3>
          <RunLinks runIds={runIds} label={index => `Run ${index + 1}`} />
        </section>
      )}
    </section>
  )
}
