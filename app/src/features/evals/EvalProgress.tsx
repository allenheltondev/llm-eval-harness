/**
 * Live view of the evaluation `evalStore` is currently following: phase,
 * a progress bar driven by `selectProgressFraction`, run counters, a compact
 * event feed, and a Cancel button.
 *
 * Reads straight from `evalStore` (no props) — rendered only while an
 * evaluation is active, mirroring how `OutputPane` reads `runStore` directly.
 */

import {
  Button,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  CardTitle,
  LoadingSpinner
} from '@readysetcloud/ui'
import ProgressBar from '../../components/ProgressBar'
import { selectProgressFraction, useEvalStore, type EvalPhase } from '../../stores'
import type { EvalStreamEvent } from '../../api'

const PHASE_LABELS: Record<EvalPhase, string> = {
  idle: 'Idle',
  starting: 'Starting…',
  running: 'Running',
  grading: 'Grading',
  completed: 'Completed',
  error: 'Error',
  cancelled: 'Cancelled'
}

/** Prefer the human message; fall back to the error code. */
function describeError(error: Record<string, unknown> | null): string {
  if (!error) return 'unknown error'
  if (typeof error.message === 'string' && error.message !== '') return error.message
  if (typeof error.code === 'string' && error.code !== '') return error.code
  return 'unknown error'
}

interface FeedEntry {
  failed: boolean
  line: string
}

/** Project the log down to the two event types the compact feed shows. */
function buildFeed(events: EvalStreamEvent[]): FeedEntry[] {
  const entries: FeedEntry[] = []
  for (const event of events) {
    if (event.type === 'run_completed') {
      const { output_chars, tool_calls, duration_ms } = event.summary
      entries.push({
        failed: false,
        line: `Run ${event.index + 1} completed (${output_chars} chars, ${tool_calls} tools, ${duration_ms}ms)`
      })
    } else if (event.type === 'run_failed') {
      entries.push({
        failed: true,
        line: `Run ${event.index + 1} failed (${describeError(event.error)})`
      })
    }
  }
  return entries
}

export default function EvalProgress() {
  const status = useEvalStore(state => state.status)
  const progress = useEvalStore(state => state.progress)
  const events = useEvalStore(state => state.events)
  const fraction = useEvalStore(selectProgressFraction)
  const cancelEvaluation = useEvalStore(state => state.cancelEvaluation)

  const feed = buildFeed(events)

  return (
    <Card role="region" aria-labelledby="eval-progress-heading" data-testid="eval-progress">
      <CardHeader className="flex items-center justify-between gap-3">
        <CardTitle id="eval-progress-heading">{PHASE_LABELS[status]}</CardTitle>
        {status === 'grading' && (
          <span className="inline-flex items-center gap-2 text-sm text-muted-foreground">
            <LoadingSpinner size="sm" />
            Grading…
          </span>
        )}
      </CardHeader>

      <CardBody>
        <ProgressBar
          progress={fraction * 100}
          status={`${progress.completed + progress.failed} / ${progress.total} runs`}
          indeterminate={false}
          color={progress.failed > 0 ? 'warning' : 'primary'}
        />

        <dl className="grid grid-cols-3 gap-3 mt-4 text-sm">
          <div>
            <dt className="text-xs text-muted-foreground">Completed</dt>
            <dd className="font-medium text-foreground" data-testid="eval-progress-completed">
              {progress.completed}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Failed</dt>
            <dd className="font-medium text-foreground" data-testid="eval-progress-failed">
              {progress.failed}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Total</dt>
            <dd className="font-medium text-foreground" data-testid="eval-progress-total">
              {progress.total}
            </dd>
          </div>
        </dl>

        <ul
          className="mt-4 space-y-1 max-h-48 overflow-y-auto text-xs font-mono"
          data-testid="eval-event-feed"
        >
          {feed.length === 0 && (
            <li className="text-muted-foreground font-sans">Waiting for the first run…</li>
          )}
          {feed.map((entry, index) => (
            <li key={index} className={entry.failed ? 'text-error-600' : 'text-muted-foreground'}>
              {entry.line}
            </li>
          ))}
        </ul>
      </CardBody>

      <CardFooter>
        <Button variant="secondary" onClick={() => void cancelEvaluation()}>
          Cancel
        </Button>
      </CardFooter>
    </Card>
  )
}
