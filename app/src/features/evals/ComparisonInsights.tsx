/**
 * What the numbers cannot tell you, said plainly.
 *
 * Two pieces: the *effects* of a models-by-prompts grid (which axis moves the
 * pass rate more, when the arms are a complete grid), and the *warnings* about
 * how far to trust a comparison (a single repeat, a small suite, a judge from
 * the same family as an arm, tools never asserted on, model and prompt changed
 * together).
 */

import { Alert, Card, CardBody, CardHeader } from '@readysetcloud/ui'
import type { ComparisonEffects, ComparisonWarning } from '../../api'
import { percent, points } from './comparisonFormat'

export function EffectsPanel({ effects }: { effects: ComparisonEffects }) {
  const names = Object.keys(effects.axes)
  const conclusion = effects.dominant
    ? `The ${effects.dominant} moves the pass rate more than the ${names.find(name => name !== effects.dominant) ?? 'other axis'} does.`
    : 'Neither axis clearly moves the pass rate more than the other.'

  return (
    <Card role="region" aria-labelledby="effects-heading" data-testid="effects-panel">
      <CardHeader>
        <h3 id="effects-heading" className="text-sm font-semibold">
          Which matters more: the model or the prompt?
        </h3>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-sm" data-testid="effects-conclusion">
          {conclusion}
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {names.map(name => (
            <section key={name} aria-label={`Effect of ${name}`} data-testid={`effect-${name}`}>
              <h4 className="text-xs font-semibold">
                <span className="capitalize">{name}</span>{' '}
                <span className="font-normal text-muted-foreground">
                  (spread {points(effects.spread[name])})
                </span>
              </h4>
              <ul className="mt-1 space-y-1">
                {effects.axes[name].map(entry => (
                  <li key={entry.value} className="text-sm">
                    <div className="flex justify-between gap-2">
                      <span className="break-words">{entry.value}</span>
                      <span className="font-medium">{percent(entry.mean_pass_rate)}</span>
                    </div>
                    <div className="h-1.5 rounded bg-muted" aria-hidden="true">
                      <div
                        className="h-1.5 rounded bg-primary-600"
                        style={{ width: `${Math.round(entry.mean_pass_rate * 100)}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Each figure is the mean pass rate over every arm with that value. It does not separate a
          prompt that helps only one model.
        </p>
      </CardBody>
    </Card>
  )
}

export function WarningsList({ warnings }: { warnings: ComparisonWarning[] }) {
  if (warnings.length === 0) return null
  return (
    <Alert variant="info" data-testid="comparison-warnings">
      <p className="font-medium">Before you rely on this</p>
      <ul className="mt-1 list-disc pl-5 space-y-1 text-sm">
        {warnings.map(warning => (
          <li key={warning.code} data-testid={`warning-${warning.code}`}>
            {warning.message}
            {warning.arms.length > 0 && (
              <span className="text-muted-foreground"> ({warning.arms.join(', ')})</span>
            )}
          </li>
        ))}
      </ul>
    </Alert>
  )
}
