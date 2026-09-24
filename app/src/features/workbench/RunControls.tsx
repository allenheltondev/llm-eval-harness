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
    <section className="card p-4 sm:p-6" aria-labelledby="run-controls-heading">
      <h2 id="run-controls-heading" className="text-base font-semibold text-gray-900 mb-3">
        Run
      </h2>

      <div className="space-y-3">
        <div>
          <label htmlFor="toolset-select" className="block text-xs font-medium text-gray-700 mb-1">
            Tools
          </label>
          <select
            id="toolset-select"
            className="input"
            value={toolset ?? ''}
            onChange={event => setToolset(event.target.value === '' ? null : event.target.value)}
          >
            <option value="">None</option>
            {toolsets.map(entry => (
              <option key={entry.name} value={entry.name}>
                {entry.name}
              </option>
            ))}
          </select>
          {selectedToolset && (
            <p className="mt-1 text-xs text-gray-500" data-testid="toolset-tools">
              {selectedToolset.tools.length > 0
                ? selectedToolset.tools.join(', ')
                : 'No tools in this toolset'}
            </p>
          )}
          {toolsError && (
            <p className="mt-1 text-xs text-red-600" role="alert">
              Could not load toolsets: {toolsError}
            </p>
          )}
        </div>

        <div className={toolsEnabled ? '' : 'opacity-50'}>
          <label
            htmlFor="max-tool-iterations"
            className="block text-xs font-medium text-gray-700 mb-1"
          >
            Max tool iterations
          </label>
          <input
            id="max-tool-iterations"
            type="number"
            min={1}
            max={100}
            className="input"
            disabled={!toolsEnabled}
            value={maxToolIterations}
            onChange={event => setMaxToolIterations(Number(event.target.value) || 1)}
          />
        </div>

        <div>
          <label
            htmlFor="guardrail-select"
            className="block text-xs font-medium text-gray-700 mb-1"
          >
            Guardrail
          </label>
          <select
            id="guardrail-select"
            className="input disabled:opacity-50 disabled:cursor-not-allowed"
            value={guardrail?.id ?? ''}
            disabled={guardrailBlocked}
            title={guardrailBlocked ? guardrailHint : undefined}
            onChange={event =>
              setGuardrail(
                event.target.value === '' ? null : { id: event.target.value, trace: true }
              )
            }
          >
            <option value="">None</option>
            {attachable.map(row => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
          {guardrailBlocked && <p className="mt-1 text-xs text-gray-500">{guardrailHint}</p>}
        </div>

        <div>
          <button
            type="button"
            className="text-xs font-medium text-primary-700 hover:text-primary-800"
            aria-expanded={showInference}
            aria-controls="inference-fields"
            onClick={() => setShowInference(open => !open)}
          >
            {showInference ? '▾' : '▸'} Inference parameters
          </button>

          {showInference && (
            <div
              id="inference-fields"
              className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 p-3 rounded-lg border border-gray-200 bg-gray-50"
            >
              <div>
                <label htmlFor="temperature" className="block text-xs text-gray-700 mb-1">
                  Temperature
                </label>
                <input
                  id="temperature"
                  type="number"
                  step="0.1"
                  min={0}
                  max={1}
                  className="input"
                  value={inference.temperature ?? ''}
                  onChange={event =>
                    setInference({ temperature: toOptionalNumber(event.target.value) })
                  }
                />
              </div>
              <div>
                <label htmlFor="top-p" className="block text-xs text-gray-700 mb-1">
                  Top P
                </label>
                <input
                  id="top-p"
                  type="number"
                  step="0.05"
                  min={0}
                  max={1}
                  className="input"
                  value={inference.top_p ?? ''}
                  onChange={event => setInference({ top_p: toOptionalNumber(event.target.value) })}
                />
              </div>
              <div>
                <label htmlFor="max-tokens" className="block text-xs text-gray-700 mb-1">
                  Max tokens
                </label>
                <input
                  id="max-tokens"
                  type="number"
                  min={1}
                  className="input"
                  value={inference.max_tokens ?? ''}
                  onChange={event =>
                    setInference({ max_tokens: toOptionalNumber(event.target.value) })
                  }
                />
              </div>
            </div>
          )}
        </div>

        <div className="flex gap-2 pt-1">
          <button
            type="button"
            className="btn btn-primary flex-1 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={!canRun || isRunning}
            onClick={handleRun}
          >
            {isRunning ? 'Running…' : 'Run'}
          </button>
          <button
            type="button"
            className="btn btn-secondary disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={!isRunning}
            onClick={() => cancelRun()}
          >
            Cancel
          </button>
        </div>
      </div>
    </section>
  )
}
