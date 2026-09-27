/**
 * How an estimated cost reads everywhere it is shown.
 *
 * The server's costs are estimates from a list-price table
 * (`server/nimbus/pricing.py`), and a model with no known price reports
 * `null`: that is "unknown", never `$0`. A value that is absent altogether (a
 * run recorded before costs existed, or one that never reported metrics) is
 * shown as a plain dash.
 */

export const UNKNOWN_COST = 'unknown'

/** Anything positive but under this reads `<$0.0001`, never `$0.0000`. */
const SMALLEST_SHOWN = 0.0001

/**
 * `$0.0123` under a dollar, `$12.50` above it; `unknown` for `null`, `—` when
 * absent. A positive cost too small for four decimals reads `<$0.0001`: it
 * was not free, and `$0.0000` would say it was.
 */
export function formatUsd(value: unknown): string {
  if (value === null) return UNKNOWN_COST
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value > 0 && value < SMALLEST_SHOWN) return '<$0.0001'
  if (value < 1) return `$${value.toFixed(4)}`
  return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** Hover text for any displayed cost. */
export const COST_ESTIMATE_NOTE =
  'Estimated from list prices per million tokens; unknown when the model has no known price.'
