/**
 * The model catalog the workbench and the evaluation launcher pick from.
 *
 * `loadModels` is idempotent: a second call while the first is in flight
 * returns the *same* promise (so mounting three components that all "ensure
 * models are loaded" issues one request), and a completed load is not repeated
 * unless `force` is passed. The in-flight promise lives at module scope rather
 * than in state — it is not serializable and nothing renders it.
 */

import { create } from 'zustand'
import { api } from '../api'
import type { ModelInfo, ModelProviders, ModelSource } from '../api'
import { isAborted, toStoreError, type StoreError } from './errors'

export interface ModelStateData {
  models: ModelInfo[]
  modelsLoading: boolean
  modelsLoaded: boolean
  modelsError: StoreError | null
  /** `ModelListResponse.cached` — the server served this from its catalog cache. */
  modelsCached: boolean
  /** `ModelListResponse.providers`, as last fetched; `null` until `loadModels` resolves. */
  modelProviders: ModelProviders | null
}

export interface ModelActions {
  /** `GET /models`. Idempotent unless `force`. */
  loadModels(force?: boolean): Promise<void>
  clear(): void
}

export type ModelStore = ModelStateData & ModelActions

export const INITIAL_MODEL_STATE: ModelStateData = {
  models: [],
  modelsLoading: false,
  modelsLoaded: false,
  modelsError: null,
  modelsCached: false,
  modelProviders: null
}

/** Idempotence guard — the one in-flight request, if any. */
let modelsRequest: Promise<void> | null = null

export const useModelStore = create<ModelStore>()((set, get) => ({
  ...INITIAL_MODEL_STATE,

  loadModels: (force = false) => {
    if (!force) {
      if (modelsRequest) return modelsRequest
      if (get().modelsLoaded) return Promise.resolve()
    }

    set({ modelsLoading: true, modelsError: null })
    modelsRequest = (async () => {
      try {
        const response = await api.models.list()
        set({
          models: response.models,
          modelsCached: response.cached,
          modelProviders: response.providers,
          modelsLoading: false,
          modelsLoaded: true
        })
      } catch (error) {
        if (isAborted(error)) {
          set({ modelsLoading: false })
          return
        }
        set({ modelsLoading: false, modelsError: toStoreError(error) })
      } finally {
        modelsRequest = null
      }
    })()
    return modelsRequest
  },

  clear: () => {
    modelsRequest = null
    set({ ...INITIAL_MODEL_STATE })
  }
}))

/** Pure helper: find a model row by id. */
export function findModel(models: ModelInfo[], modelId: string): ModelInfo | null {
  return models.find(model => model.model_id === modelId) ?? null
}

/* -------------------------------------------------------------------------- */
/* Model source grouping — shared by ModelPanel and DeterminismLauncher       */
/* -------------------------------------------------------------------------- */

const SOURCE_ORDER: ModelSource[] = ['bedrock', 'anthropic', 'openai', 'ollama']

const SOURCE_LABELS: Record<ModelSource, string> = {
  bedrock: 'Bedrock',
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  ollama: 'Ollama (local)'
}

/** Whether a source's models are currently selectable. */
function sourceUsable(source: ModelSource, providers: ModelProviders): boolean {
  const status = providers[source]
  if (!status.configured) return false
  if (source === 'ollama' && providers.ollama.reachable === false) return false
  return true
}

/** Suffix appended to a source's group label when it isn't usable. */
function sourceSuffix(source: ModelSource, providers: ModelProviders): string {
  const status = providers[source]
  if (!status.configured) return ' (not configured)'
  if (source === 'ollama' && providers.ollama.reachable === false) return ' (unreachable)'
  return ''
}

export interface ModelSourceGroup {
  source: ModelSource
  /** Display label, with a "(not configured)"/"(unreachable)" suffix when relevant. */
  label: string
  /** True when the provider is unconfigured (or ollama is unreachable). */
  disabled: boolean
  models: ModelInfo[]
}

export interface GroupedModels {
  /** One entry per source that has at least one catalog row, in a fixed order. */
  groups: ModelSourceGroup[]
  /**
   * Sources with *no* catalog rows at all that are also unusable — nothing to
   * group, so callers render these as a footnote instead of an empty optgroup.
   */
  unavailable: Array<{ source: ModelSource; label: string }>
}

/**
 * Group a flat model catalog into per-source buckets (Bedrock / Anthropic /
 * OpenAI / Ollama (local)), in that fixed order, folding in `providers` to
 * mark unconfigured/unreachable sources. With `providers` still `null` (the
 * catalog has not loaded) there is nothing to group and nothing to report.
 *
 * Pure and framework-free so `ModelPanel` and `DeterminismLauncher` share one
 * implementation instead of two ad hoc `<select>` groupings.
 */
export function groupModelsBySource(
  models: ModelInfo[],
  providers: ModelProviders | null
): GroupedModels {
  if (providers === null) return { groups: [], unavailable: [] }
  const resolved = providers

  const bySource = new Map<ModelSource, ModelInfo[]>()
  for (const model of models) {
    const source = model.source
    const existing = bySource.get(source)
    if (existing) existing.push(model)
    else bySource.set(source, [model])
  }

  const groups: ModelSourceGroup[] = []
  const unavailable: Array<{ source: ModelSource; label: string }> = []

  for (const source of SOURCE_ORDER) {
    const sourceModels = bySource.get(source)
    if (sourceModels && sourceModels.length > 0) {
      groups.push({
        source,
        label: `${SOURCE_LABELS[source]}${sourceSuffix(source, resolved)}`,
        disabled: !sourceUsable(source, resolved),
        models: sourceModels
      })
    } else if (!sourceUsable(source, resolved)) {
      unavailable.push({ source, label: SOURCE_LABELS[source] })
    }
  }

  return { groups, unavailable }
}
