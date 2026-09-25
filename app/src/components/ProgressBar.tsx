export interface ProgressBarProps {
  /** Progress percentage (0-100) */
  progress?: number
  /** Current status text */
  status?: string
  /** Whether progress is indeterminate */
  indeterminate?: boolean
  /** Color theme */
  color?: 'primary' | 'success' | 'warning' | 'error'
}

// Token classes from the @readysetcloud/ui preset: the fill uses the tone's
// solid stop and the track its muted stop, so both themes come from tokens.
const FILL_CLASSES = {
  primary: 'bg-primary-600',
  success: 'bg-success-600',
  warning: 'bg-warning-600',
  error: 'bg-error-600'
} as const

const TRACK_CLASSES = {
  primary: 'bg-primary-100',
  success: 'bg-success-100',
  warning: 'bg-warning-100',
  error: 'bg-error-100'
} as const

/**
 * ProgressBar component for showing progress during operations
 */
function ProgressBar({
  progress = 0,
  status,
  indeterminate = false,
  color = 'primary'
}: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, progress))

  return (
    <div className="w-full">
      {status && (
        <div className="flex justify-between items-center mb-2">
          <span className="text-sm font-medium text-foreground">{status}</span>
          {!indeterminate && (
            <span className="text-sm text-muted-foreground">{Math.round(progress)}%</span>
          )}
        </div>
      )}

      <div
        className={`w-full h-2 rounded-full ${TRACK_CLASSES[color]}`}
        role="progressbar"
        aria-label={status ?? 'Progress'}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : Math.round(clamped)}
      >
        <div
          className={`h-2 rounded-full transition-all duration-300 ease-out ${FILL_CLASSES[color]} ${
            indeterminate ? 'animate-pulse' : ''
          }`}
          style={{ width: indeterminate ? '100%' : `${clamped}%` }}
        />
      </div>
    </div>
  )
}

export default ProgressBar
