/**
 * Token / latency / cycle strip for the current run.
 *
 * Elapsed time is read from `startedAt`/`endedAt` on every render rather than
 * ticked on a timer: while a run is streaming the store notifies often enough
 * for the number to move, and a finished run freezes it at `endedAt`.
 */

import { StatTile } from '@readysetcloud/ui'
import { useRunStore } from '../../stores'

interface StatProps {
  label: string
  value: string
}

/** One tile; `role="group"` + label so each value is announced with its name. */
function Stat({ label, value }: StatProps) {
  return (
    <StatTile
      role="group"
      aria-label={label}
      className="min-w-0 tabular-nums"
      label={label}
      value={value}
    />
  )
}

function formatCount(value: number | undefined): string {
  return value === undefined ? '—' : value.toLocaleString()
}

function formatMs(value: number | undefined): string {
  if (value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`
}

export default function MetricsBar() {
  const metrics = useRunStore(state => state.metrics)
  const startedAt = useRunStore(state => state.startedAt)
  const endedAt = useRunStore(state => state.endedAt)

  // Computed in render, not via `selectElapsedMs`: that selector calls
  // `Date.now()`, so as a zustand selector it would return a new value on every
  // snapshot comparison and spin `useSyncExternalStore`.
  const elapsed = startedAt === null ? undefined : (endedAt ?? Date.now()) - startedAt

  return (
    <section aria-labelledby="metrics-bar-heading">
      <h2 id="metrics-bar-heading" className="sr-only">
        Run metrics
      </h2>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        <Stat label="Input tokens" value={formatCount(metrics?.input_tokens)} />
        <Stat label="Output tokens" value={formatCount(metrics?.output_tokens)} />
        <Stat label="Total tokens" value={formatCount(metrics?.total_tokens)} />
        <Stat label="Latency" value={formatMs(metrics?.latency_ms)} />
        <Stat label="Cycles" value={formatCount(metrics?.cycle_count)} />
        <Stat label="Elapsed" value={formatMs(elapsed)} />
      </div>
    </section>
  )
}
