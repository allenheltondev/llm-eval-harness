/**
 * The workbench form: everything that goes into `POST /runs`.
 *
 * This store is *only* form state — it never calls the API. `runStore.startRun`
 * takes the request built from here (see `toRunRequest`), which keeps the form
 * editable while a run is in flight and makes "re-run this exact config"
 * trivial.
 *
 * Persisted to localStorage under `evalharness.run-config`. Everything in
 * the state is plain configuration (no credentials, no outputs), so
 * `partialize` keeps all of it and drops only the action functions.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { InferenceConfig, ModelSource, RunGuardrailConfig, RunRequest } from '../api'

/** localStorage key. */
export const RUN_CONFIG_STORAGE_KEY = 'evalharness.run-config'

/** The serializable half of the store (this is exactly what is persisted). */
export interface RunConfigData {
  model_id: string
  /**
   * Which backend `model_id` is served from. Guardrails only work with
   * `'bedrock'` — see `setProvider`, which clears `guardrail` whenever this
   * is set to anything else, keeping that invariant enforced in one place.
   */
  provider: ModelSource
  system_prompt: string
  user_prompt: string
  inference: InferenceConfig
  /** A toolset name from `GET /tools`, or `null` to run without tools. */
  toolset: string | null
  /** 1 – 100. */
  max_tool_iterations: number
  guardrail: RunGuardrailConfig | null
  stream: boolean
}

export interface RunConfigActions {
  setModelId(modelId: string): void
  /**
   * Sets `provider` alone (e.g. from the manual-model-id fallback's provider
   * select). Switching away from `'bedrock'` clears `guardrail`, since a
   * guardrail with a non-bedrock provider is a server-side 400.
   */
  setProvider(provider: ModelSource): void
  /**
   * Sets `model_id` and `provider` together, as the catalog picker does
   * (`ModelInfo.source` -> `provider`). Applies the same guardrail-clearing
   * invariant as `setProvider`.
   */
  selectModel(modelId: string, source: ModelSource): void
  setSystemPrompt(text: string): void
  setUserPrompt(text: string): void
  /** Shallow-merges into `inference`; `undefined` values delete the key. */
  setInference(patch: Partial<InferenceConfig>): void
  setToolset(toolset: string | null): void
  setMaxToolIterations(iterations: number): void
  setGuardrail(guardrail: RunGuardrailConfig | null): void
  setStream(stream: boolean): void
  /** Back to `DEFAULT_RUN_CONFIG` (also rewrites the persisted copy). */
  reset(): void
}

export type RunConfigStore = RunConfigData & RunConfigActions

export const DEFAULT_RUN_CONFIG: RunConfigData = {
  model_id: '',
  provider: 'bedrock',
  system_prompt: '',
  user_prompt: '',
  inference: {},
  toolset: null,
  max_tool_iterations: 10,
  guardrail: null,
  stream: true
}

/**
 * Build the `POST /runs` body from form state.
 *
 * Pure and standalone so evaluations (`kind: "determinism"` needs a
 * `run_config`) can reuse it without going through `runStore`. Empty optional
 * fields are omitted rather than sent as `""`/`null` noise; `toolset` is the
 * exception, since `null` is its documented "no tools" value.
 */
export function toRunRequest(config: RunConfigData): RunRequest {
  const request: RunRequest = {
    model_id: config.model_id,
    user_prompt: config.user_prompt,
    toolset: config.toolset,
    max_tool_iterations: config.max_tool_iterations,
    // Always included (not just when non-default): a simpler, contract-legal
    // request shape beats the marginal byte savings of omitting 'bedrock'.
    provider: config.provider,
    stream: config.stream
  }
  if (config.system_prompt.trim() !== '') request.system_prompt = config.system_prompt
  if (Object.keys(config.inference).length > 0) request.inference = { ...config.inference }
  if (config.guardrail) request.guardrail = { ...config.guardrail }
  return request
}

/**
 * Guardrails only run against Bedrock (a non-bedrock provider + guardrail is
 * a server-side 400). Central place that enforces it: any state change that
 * sets `provider` should route through this so `guardrail` never gets left
 * pointing at a now-incompatible provider.
 */
function providerPatch(
  current: Pick<RunConfigData, 'guardrail'>,
  provider: ModelSource
): Pick<RunConfigData, 'provider' | 'guardrail'> {
  return {
    provider,
    guardrail: provider === 'bedrock' ? current.guardrail : null
  }
}

export const useRunConfigStore = create<RunConfigStore>()(
  persist(
    (set, get) => ({
      ...DEFAULT_RUN_CONFIG,

      setModelId: modelId => set({ model_id: modelId }),
      setProvider: provider => set(state => providerPatch(state, provider)),
      selectModel: (modelId, source) =>
        set(state => ({ model_id: modelId, ...providerPatch(state, source) })),
      setSystemPrompt: text => set({ system_prompt: text }),
      setUserPrompt: text => set({ user_prompt: text }),

      setInference: patch => {
        const inference: InferenceConfig = { ...get().inference }
        for (const [key, value] of Object.entries(patch)) {
          if (value === undefined) delete inference[key as keyof InferenceConfig]
          else inference[key as keyof InferenceConfig] = value
        }
        set({ inference })
      },

      setToolset: toolset => set({ toolset }),
      setMaxToolIterations: iterations => set({ max_tool_iterations: iterations }),
      setGuardrail: guardrail => set({ guardrail }),
      setStream: stream => set({ stream }),

      reset: () => set({ ...DEFAULT_RUN_CONFIG })
    }),
    {
      name: RUN_CONFIG_STORAGE_KEY,
      partialize: (state): RunConfigData => ({
        model_id: state.model_id,
        provider: state.provider,
        system_prompt: state.system_prompt,
        user_prompt: state.user_prompt,
        inference: state.inference,
        toolset: state.toolset,
        max_tool_iterations: state.max_tool_iterations,
        guardrail: state.guardrail,
        stream: state.stream
      })
    }
  )
)

/**
 * Selector: whether the form has the minimum needed to submit a run.
 *
 * (There is deliberately no `selectRunRequest` selector: `toRunRequest` builds
 * a fresh object, and a zustand v5 selector that does that fails
 * `useSyncExternalStore`'s snapshot identity check. Call `toRunRequest` in the
 * submit handler with `useRunConfigStore.getState()`.)
 */
export const selectCanRun = (state: RunConfigStore): boolean =>
  state.model_id.trim() !== '' && state.user_prompt.trim() !== ''
