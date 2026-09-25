/**
 * The streamed model output: a status badge, the text as it arrives (pinned to
 * the bottom while it grows), and a collapsible reasoning pane.
 *
 * Auto-scroll only re-pins when the reader is already near the bottom, so
 * scrolling up to re-read something mid-stream is not fought by the next token.
 */

import { useEffect, useRef } from 'react'
import { Alert, Card, CardBody, CardHeader, EmptyState, StatusBadge } from '@readysetcloud/ui'
import { statusTone } from '../../components/status'
import { selectIsRunning, useRunStore, type RunPhase } from '../../stores'

const STATUS_LABELS: Record<RunPhase, string> = {
  idle: 'Idle',
  starting: 'Starting',
  streaming: 'Streaming',
  completed: 'Completed',
  error: 'Error',
  cancelled: 'Cancelled'
}

/** Within this many pixels of the bottom counts as "following the stream". */
const PIN_THRESHOLD_PX = 48

export default function OutputPane() {
  const status = useRunStore(state => state.status)
  const streamedText = useRunStore(state => state.streamedText)
  const reasoningText = useRunStore(state => state.reasoningText)
  const error = useRunStore(state => state.error)
  const isRunning = useRunStore(selectIsRunning)

  const scrollRef = useRef<HTMLDivElement | null>(null)
  const pinnedRef = useRef(true)

  useEffect(() => {
    const node = scrollRef.current
    if (!node || !pinnedRef.current) return
    node.scrollTop = node.scrollHeight
  }, [streamedText])

  function handleScroll() {
    const node = scrollRef.current
    if (!node) return
    pinnedRef.current = node.scrollHeight - node.scrollTop - node.clientHeight <= PIN_THRESHOLD_PX
  }

  return (
    <Card role="region" aria-labelledby="output-pane-heading">
      <CardHeader className="flex items-center justify-between">
        <h2 id="output-pane-heading" className="card-title">
          Output
        </h2>
        <StatusBadge data-testid="run-status-badge" tone={statusTone(status)}>
          {STATUS_LABELS[status]}
        </StatusBadge>
      </CardHeader>
      <CardBody>
        {error && (
          <Alert variant="error" className="mb-3">
            <p className="text-xs font-mono">{error.code}</p>
            <p className="text-sm">{error.message}</p>
          </Alert>
        )}

        <div
          ref={scrollRef}
          onScroll={handleScroll}
          data-testid="output-text"
          className="h-72 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted p-3 font-mono text-sm text-foreground"
        >
          {streamedText === '' ? (
            <EmptyState
              title={
                isRunning ? 'Waiting for the first token…' : 'Run a prompt to see output here.'
              }
            />
          ) : (
            streamedText
          )}
        </div>

        {reasoningText !== '' && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
              Reasoning ({reasoningText.length} chars)
            </summary>
            <div className="mt-2 max-h-56 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-surface p-3 font-mono text-xs text-muted-foreground">
              {reasoningText}
            </div>
          </details>
        )}
      </CardBody>
    </Card>
  )
}
