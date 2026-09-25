/**
 * The tool call timeline.
 *
 * Each row is one `ToolEventEntry` — already merged by `runStore` from
 * `tool_use_start` + `tool_input_delta`* + `tool_result` — so a call that is
 * still streaming its arguments renders the partial JSON and gains its result
 * and duration in place when the result lands.
 */

import { Badge, Card, CardBody, CardHeader, EmptyState } from '@readysetcloud/ui'
import { useRunStore, type ToolEventEntry } from '../../stores'

/**
 * Best-effort pretty print. Tool input arrives as a JSON *fragment* stream, so
 * a mid-flight entry will not parse — show the raw text rather than an error.
 */
function prettyJson(value: unknown): string {
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return value
    }
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function ToolRow({ entry }: { entry: ToolEventEntry }) {
  const pending = entry.result === undefined && !entry.error
  const input = entry.input !== undefined ? entry.input : entry.inputJson

  return (
    <li className="rounded-lg border border-border bg-surface p-3" data-testid="tool-event">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="font-mono text-sm font-medium text-foreground">
          {entry.name || 'unnamed tool'}
        </span>
        <span className="flex items-center gap-2">
          {entry.error && <Badge variant="error">error</Badge>}
          {pending && <Badge variant="warning">running</Badge>}
          {entry.duration_ms !== undefined && (
            <span className="text-xs text-muted-foreground">{entry.duration_ms} ms</span>
          )}
        </span>
      </div>

      <div className="text-xs">
        <p className="font-medium text-muted-foreground mb-1">Input</p>
        <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-foreground">
          {prettyJson(input)}
        </pre>
      </div>

      {entry.result !== undefined && (
        <div className="text-xs mt-2">
          <p className="font-medium text-muted-foreground mb-1">Result</p>
          <pre className="max-h-48 overflow-auto rounded bg-muted p-2 font-mono text-foreground">
            {prettyJson(entry.result)}
          </pre>
        </div>
      )}

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

export default function ToolTimeline() {
  const toolEvents = useRunStore(state => state.toolEvents)

  return (
    <Card role="region" aria-labelledby="tool-timeline-heading">
      <CardHeader>
        <h2 id="tool-timeline-heading" className="card-title">
          Tool calls{toolEvents.length > 0 && ` (${toolEvents.length})`}
        </h2>
      </CardHeader>
      <CardBody>
        {toolEvents.length === 0 ? (
          <EmptyState title="No tools were called." />
        ) : (
          <ul className="space-y-2">
            {toolEvents.map(entry => (
              <ToolRow key={entry.tool_use_id} entry={entry} />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}
