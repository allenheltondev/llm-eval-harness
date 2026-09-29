/**
 * How the numbers in a comparison read.
 *
 * Kept apart from the components so the rules (a missing figure is a dash,
 * never zero; a change of pass rate is in points) are one place and one test.
 */

/** `0.9` -> `90%`; a missing rate is a dash. */
export function percent(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? '—' : `${Math.round(rate * 100)}%`
}

/** A change of a 0 – 1 rate in percentage points, signed: `+5 pts`, `−10 pts`, `±0 pts`. */
export function points(delta: number | null | undefined): string {
  if (delta === null || delta === undefined) return '—'
  const rounded = Math.round(delta * 100)
  if (rounded === 0) return '±0 pts'
  return `${rounded > 0 ? '+' : '−'}${Math.abs(rounded)} pts`
}

/** A change of a score (0 – 100), signed. */
export function scoreDelta(delta: number | null | undefined): string {
  if (delta === null || delta === undefined) return '—'
  if (delta === 0) return '±0'
  return `${delta > 0 ? '+' : '−'}${Math.abs(delta)}`
}

/** A ratio to the baseline: `1.8×`; missing when either side was not measured. */
export function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(1)}×`
}

/** Milliseconds as a person reads a delay: `850 ms`, `2.4 s`. */
export function duration(milliseconds: number | null | undefined): string {
  if (milliseconds === null || milliseconds === undefined) return '—'
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`
}

/** A probability for the reader, without false precision: `0.62`, or `<0.01`. */
export function probability(p: number): string {
  return p < 0.01 ? '<0.01' : p.toFixed(2)
}
