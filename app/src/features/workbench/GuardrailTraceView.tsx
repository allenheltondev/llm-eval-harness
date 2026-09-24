/**
 * The guardrail assessment for the current run, as pretty JSON.
 *
 * The trace shape is Bedrock's and changes with the policies configured on the
 * guardrail, so this renders it verbatim rather than pretending to know the
 * schema. It collapses itself away entirely when no trace arrived.
 */

import { Card, CardBody } from '@readysetcloud/ui'
import { useRunStore } from '../../stores'

export default function GuardrailTraceView() {
  const guardrailTrace = useRunStore(state => state.guardrailTrace)

  if (!guardrailTrace) return null

  return (
    <Card role="region" aria-labelledby="guardrail-trace-heading">
      <CardBody>
        <details>
          <summary id="guardrail-trace-heading" className="card-title cursor-pointer">
            Guardrail trace
          </summary>
          <pre
            data-testid="guardrail-trace-json"
            className="mt-3 max-h-72 overflow-auto rounded-lg border border-border bg-muted p-3 font-mono text-xs text-foreground"
          >
            {JSON.stringify(guardrailTrace, null, 2)}
          </pre>
        </details>
      </CardBody>
    </Card>
  )
}
