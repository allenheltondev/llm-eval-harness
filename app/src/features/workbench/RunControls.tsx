/**
 * Everything between "the prompt is written" and "the run is in flight":
 * inference knobs, the toolset picker, guardrail selection, and Run / Cancel.
 *
 * The submit handler deliberately reads `useRunConfigStore.getState()` rather
 * than subscribing to the whole config — `toRunRequest` builds a fresh object,
 * which cannot be a zustand v5 selector, and the button only needs the boolean
 * from `selectCanRun`.
 *
 * Toolsets come from `GET /tools`, fetched once on mount and held locally: the
 * list is static for the life of the server and nothing else reads it.
 *
 * Guardrails only run against the `bedrock` provider (a guardrail + a
 * non-bedrock provider is a server-side 400), so the guardrail select is
 * disabled whenever `provider !== 'bedrock'`. The complementary half of the
 * invariant — clearing an already-selected guardrail when the provider
 * changes away from bedrock — lives centrally in `runConfigStore.setProvider`
 * / `.selectModel`, not here.
 */

import { useEffect, useState } from 'react'
import { Alert, Button, Card, CardBody, CardHeader, Input, Select } from '@readysetcloud/ui'
import { api } from '../../api'
import type { Toolset } from '../../api'
import {
  readyGuardrails,
  selectCanRun,
  selectIsRunning,
  toRunRequest,
  useGuardrailStore,
  useRunConfigStore,
  useRunStore
} from '../../stores'

/** `""` -> `undefined` (drops the key), otherwise a finite number. */
function toOptionalNumber(raw: string): number | undefined {
  if (raw.trim() === '') return undefined
  const value = Number(raw)
  return Number.isFinite(value) ? value : undefined
}

export default function RunControls() {
  const [showInference, setShowInference] = useState(false)
  const [toolsets, setToolsets] = useState<Toolset[]>([])
  const [toolsError, setToolsError] = useState<string | null>(null)

  const inference = useRunConfigStore(state => state.inference)
  const toolset = useRunConfigStore(state => state.toolset)
  const maxToolIterations = useRunConfigStore(state => state.max_tool_iterations)
  const guardrail = useRunConfigStore(state => state.guardrail)
  const provider = useRunConfigStore(state => state.provider)
  const setInference = useRunConfigStore(state => state.setInference)
  const setToolset = useRunConfigStore(state => state.setToolset)
  const setMaxToolIterations = useRunConfigStore(state => state.setMaxToolIterations)
  const setGuardrail = useRunConfigStore(state => state.setGuardrail)
  const canRun = useRunConfigStore(selectCanRun)

  const guardrails = useGuardrailStore(state => state.guardrails)
  const loadGuardrails = useGuardrailStore(state => state.loadGuardrails)

  const isRunning = useRunStore(selectIsRunning)
  const startRun = useRunStore(state => state.startRun)
  const cancelRun = useRunStore(state => state.cancelRun)

  useEffect(() => {
    void loadGuardrails()
  }, [loadGuardrails])

  useEffect(() => {
    let cancelled = false
    api
      .tools()
      .then(response => {
        if (!cancelled) setToolsets(response.toolsets)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setToolsError(error instanceof Error ? error.message : 'Could not load toolsets')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const attachable = readyGuardrails(guardrails)
  const guardrailBlocked = provider !== 'bedrock'
  const guardrailHint = 'Guardrails require the Bedrock provider'

  const toolsEnabled = toolset !== null
  const selectedToolset = toolsets.find(entry => entry.name === toolset) ?? null

  function handleRun() {
    void startRun(toRunRequest(useRunConfigStore.getState()))
  }

  return (
    <Card role="region" aria-labelledby="run-controls-heading">
      <CardHeader>
        <h2 id="run-controls-heading" className="card-title">
          Run
        </h2>
      </CardHeader>
      <CardBody className="space-y-3">
        <div className="space-y-1">
          <Select
            label="Tools"
            value={toolset ?? ''}
            onChange={event => setToolset(event.target.value === '' ? null : event.target.value)}
          >
            <option value="">None</option>
            {toolsets.map(entry => (
              <option key={entry.name} value={entry.name}>
                {entry.name}
              </option>
            ))}
          </Select>
          {selectedToolset && (
            <p className="text-xs text-muted-foreground" data-testid="toolset-tools">
              {selectedToolset.tools.length > 0
                ? selectedToolset.tools.join(', ')
                : 'No tools in this toolset'}
            </p>
          )}
          {toolsError && <Alert variant="error">Could not load toolsets: {toolsError}</Alert>}
        </div>

        <div className={toolsEnabled ? '' : 'opacity-50'}>
          <Input
            label="Max tool iterations"
            type="number"
            min={1}
            max={100}
            disabled={!toolsEnabled}
            value={maxToolIterations}
            onChange={event => setMaxToolIterations(Number(event.target.value) || 1)}
          />
        </div>

        <Select
          label="Guardrail"
          value={guardrail?.id ?? ''}
          disabled={guardrailBlocked}
          title={guardrailBlocked ? guardrailHint : undefined}
          hint={guardrailBlocked ? guardrailHint : undefined}
          onChange={event =>
            setGuardrail(event.target.value === '' ? null : { id: event.target.value, trace: true })
          }
        >
          <option value="">None</option>
          {attachable.map(row => (
            <option key={row.id} value={row.id}>
              {row.name}
            </option>
          ))}
        </Select>

        <div>
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={showInference}
            aria-controls="inference-fields"
            onClick={() => setShowInference(open => !open)}
          >
            {showInference ? '▾' : '▸'} Inference parameters
          </Button>

          {showInference && (
            <div
              id="inference-fields"
              className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 p-3 rounded-lg border border-border bg-muted"
            >
              <Input
                label="Temperature"
                type="number"
                step="0.1"
                min={0}
                max={1}
                value={inference.temperature ?? ''}
                onChange={event =>
                  setInference({ temperature: toOptionalNumber(event.target.value) })
                }
              />
              <Input
                label="Top P"
                type="number"
                step="0.05"
                min={0}
                max={1}
                value={inference.top_p ?? ''}
                onChange={event => setInference({ top_p: toOptionalNumber(event.target.value) })}
              />
              <Input
                label="Max tokens"
                type="number"
                min={1}
                value={inference.max_tokens ?? ''}
                onChange={event =>
                  setInference({ max_tokens: toOptionalNumber(event.target.value) })
                }
              />
            </div>
          )}
        </div>

        <div className="flex gap-2 pt-1">
          <Button
            className="flex-1"
            disabled={!canRun}
            loading={isRunning}
            loadingLabel="Running…"
            onClick={handleRun}
          >
            Run
          </Button>
          <Button variant="secondary" disabled={!isRunning} onClick={() => cancelRun()}>
            Cancel
          </Button>
        </div>
      </CardBody>
    </Card>
  )
}
