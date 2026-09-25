/**
 * Full detail for a single run: prompts, output, tool transcript, metrics and
 * guardrail trace.
 *
 * Fetches through `historyStore.getRunDetail`, which caches by run id — so two
 * `RunDetailView`s showing the same run (e.g. a re-open after Compare) share
 * one request. `highlight` is set by `CompareView` to flag fields that differ
 * between the two runs being compared; standalone use leaves it empty.
 */

import { useEffect } from 'react'
import { useHistoryStore } from '../../stores'
import type { RunDetail, RunMetrics, ToolTranscriptEntry } from '../../api'
import {
  Alert,
  Badge,
  Card,
  CardBody,
  ErrorState,
  Loading,
  StatTile,
  StatusBadge
} from '@readysetcloud/ui'
import { statusTone } from '../../components/status'

export interface RunDetailHighlight {
  model_id?: boolean
  status?: boolean
  metrics?: Partial<Record<keyof RunMetrics, boolean>>
}

export interface RunDetailViewProps {
  runId: string
  highlight?: RunDetailHighlight
  className?: string
}

function formatTs(ts: string): string {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString()
}

function formatCount(value: number | undefined): string {
  return value === undefined ? '—' : value.toLocaleString()
}

function formatMs(value: number | undefined): string {
  if (value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`
}

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

interface MetricStatProps {
  label: string
  value: string
  highlighted?: boolean
}

function MetricStat({ label, value, highlighted }: MetricStatProps) {
  return (
    <StatTile
      className={`min-w-0 ${highlighted ? 'ring-2 ring-warning-400' : ''}`}
      data-testid="run-detail-metric"
      data-highlighted={highlighted ? 'true' : 'false'}
      label={label}
      value={value}
    />
  )
}

function ToolTranscriptRow({ entry }: { entry: ToolTranscriptEntry }) {
  return (
    <li
      className="rounded-lg border border-border bg-surface p-3"
      data-testid="run-detail-tool-row"
    >
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="font-mono text-sm font-medium text-foreground">{entry.name}</span>
        <span className="flex items-center gap-2">
          {entry.error && <Badge variant="error">error</Badge>}
          <span className="text-xs text-muted-foreground">{entry.duration_ms} ms</span>
        </span>
      </div>
      <div className="text-xs">
        <p className="font-medium text-muted-foreground mb-1">Input</p>
        <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-foreground">
          {prettyJson(entry.input)}
        </pre>
      </div>
      <div className="text-xs mt-2">
        <p className="font-medium text-muted-foreground mb-1">Output</p>
        <pre className="max-h-48 overflow-auto rounded bg-muted p-2 font-mono text-foreground">
          {prettyJson(entry.output)}
        </pre>
      </div>
      {entry.error && (
        <div className="text-xs mt-2">
          <p className="font-medium text-error-600 mb-1">Error</p>
          <pre className="overflow-x-auto rounded bg-error-50 p-2 font-mono text-error-800">
            {prettyJson(entry.error)}
          </pre>
        </div>
      )}
    </li>
  )
}

export default function RunDetailView({ runId, highlight, className = '' }: RunDetailViewProps) {
  const detail = useHistoryStore(state => state.details[runId])
  const getRunDetail = useHistoryStore(state => state.getRunDetail)
  const error = useHistoryStore(state => state.error)

  useEffect(() => {
    void getRunDetail(runId)
  }, [runId, getRunDetail])

  if (!detail) {
    return (
      <Card className={className} data-testid="run-detail-view">
        <CardBody>
          <Loading text="Loading run…" />
          {error && (
            <ErrorState
              className="mt-3"
              heading="Could not load this run"
              message={error.message}
              action={{ label: 'Try again', onClick: () => void getRunDetail(runId, true) }}
            />
          )}
        </CardBody>
      </Card>
    )
  }

  return <RunDetailBody detail={detail} highlight={highlight} className={className} />
}

function RunDetailBody({
  detail,
  highlight,
  className
}: {
  detail: RunDetail
  highlight?: RunDetailHighlight
  className: string
}) {
  const transcript = detail.tool_transcript ?? []

  return (
    <Card className={className} data-testid="run-detail-view" data-run-id={detail.id}>
      <CardBody className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="text-xs text-muted-foreground">{formatTs(detail.ts)}</p>
            <p
              className={`font-mono text-sm text-foreground break-all rounded ${
                highlight?.model_id ? 'ring-2 ring-warning-400 bg-warning-50 px-1' : ''
              }`}
              data-testid="run-detail-model-id"
              data-highlighted={highlight?.model_id ? 'true' : 'false'}
            >
              {detail.model_id}
            </p>
            {detail.config?.toolset && (
              <p className="text-xs text-muted-foreground" data-testid="run-detail-toolset">
                Toolset: {detail.config.toolset}
              </p>
            )}
          </div>
          <StatusBadge
            data-testid="run-detail-status-badge"
            data-highlighted={highlight?.status ? 'true' : 'false'}
            tone={statusTone(detail.status)}
            role={undefined}
            className={highlight?.status ? 'ring-2 ring-warning-400' : undefined}
          >
            {detail.status}
          </StatusBadge>
        </div>

        {detail.error != null && (
          <Alert variant="error" data-testid="run-detail-error">
            <pre className="text-xs font-mono whitespace-pre-wrap break-words">
              {prettyJson(detail.error)}
            </pre>
          </Alert>
        )}

        <details>
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
            System prompt
          </summary>
          <pre className="mt-2 max-h-40 overflow-auto rounded-lg border border-border bg-muted p-3 font-mono text-xs text-foreground whitespace-pre-wrap break-words">
            {detail.system_prompt || '—'}
          </pre>
        </details>

        <details open>
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
            User prompt
          </summary>
          <pre className="mt-2 max-h-40 overflow-auto rounded-lg border border-border bg-muted p-3 font-mono text-xs text-foreground whitespace-pre-wrap break-words">
            {detail.user_prompt || '—'}
          </pre>
        </details>

        <div>
          <p className="text-xs font-medium text-muted-foreground mb-1">Output</p>
          <div
            data-testid="run-detail-output"
            className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted p-3 font-mono text-sm text-foreground"
          >
            {detail.output || <span className="text-muted-foreground">No output.</span>}
          </div>
        </div>

        <div>
          <p className="text-xs font-medium text-muted-foreground mb-2">
            Tool calls{transcript.length > 0 && ` (${transcript.length})`}
          </p>
          {transcript.length === 0 ? (
            <p className="text-xs text-muted-foreground">No tools were called.</p>
          ) : (
            <ul className="space-y-2">
              {transcript.map(entry => (
                <ToolTranscriptRow key={entry.tool_use_id} entry={entry} />
              ))}
            </ul>
          )}
        </div>

        <div>
          <p className="text-xs font-medium text-muted-foreground mb-2">Metrics</p>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            <MetricStat
              label="Input tokens"
              value={formatCount(detail.metrics?.input_tokens)}
              highlighted={highlight?.metrics?.input_tokens}
            />
            <MetricStat
              label="Output tokens"
              value={formatCount(detail.metrics?.output_tokens)}
              highlighted={highlight?.metrics?.output_tokens}
            />
            <MetricStat
              label="Total tokens"
              value={formatCount(detail.metrics?.total_tokens)}
              highlighted={highlight?.metrics?.total_tokens}
            />
            <MetricStat
              label="Latency"
              value={formatMs(detail.metrics?.latency_ms)}
              highlighted={highlight?.metrics?.latency_ms}
            />
            <MetricStat
              label="Cycles"
              value={formatCount(detail.metrics?.cycle_count)}
              highlighted={highlight?.metrics?.cycle_count}
            />
          </div>
        </div>

        {detail.guardrail_trace != null && (
          <details>
            <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
              Guardrail trace
            </summary>
            <pre
              data-testid="run-detail-guardrail-trace"
              className="mt-2 max-h-72 overflow-auto rounded-lg border border-border bg-muted p-3 font-mono text-xs text-foreground"
            >
              {prettyJson(detail.guardrail_trace)}
            </pre>
          </details>
        )}
      </CardBody>
    </Card>
  )
}
