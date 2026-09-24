/**
 * Which design-system tone each status reads as, so a status looks the same
 * on every page. Rendered with the package's `StatusBadge`; the tones follow
 * the design system's meaning: success finished well, error failed, warning
 * needs attention (waiting, degraded), primary is in progress, neutral is
 * inert.
 */

import type { StatusBadgeTone } from '@readysetcloud/ui'

const TONES: Record<string, StatusBadgeTone> = {
  // runs and evaluations
  completed: 'success',
  error: 'error',
  cancelled: 'neutral',
  pending: 'warning',
  starting: 'warning',
  running: 'primary',
  streaming: 'primary',
  grading: 'primary',
  idle: 'neutral',
  // suite cases
  passed: 'success',
  failed: 'error',
  judge_error: 'warning',
  // guardrail lifecycle (Bedrock's own upper-case names)
  READY: 'success',
  CREATING: 'warning',
  UPDATING: 'warning',
  VERSIONING: 'primary',
  FAILED: 'error',
  DELETING: 'neutral'
}

/** The tone for a status; anything unrecognised reads as neutral. */
export function statusTone(status: string): StatusBadgeTone {
  return TONES[status] ?? 'neutral'
}
