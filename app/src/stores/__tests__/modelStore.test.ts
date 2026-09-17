/** modelStore: the idempotent catalog load and the source grouping helpers. */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelInfo, ModelListResponse, ModelProviders } from '../../api'

const modelsListMock = vi.fn()

vi.mock('../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      models: { ...actual.api.models, list: modelsListMock }
    }
  }
})

const { ApiError, StreamAbortedError } = await import('../../api')
const { useModelStore, findModel, groupModelsBySource, INITIAL_MODEL_STATE } =
  await import('../modelStore')

const modelProviders: ModelProviders = {
  bedrock: { configured: true },
  anthropic: { configured: true },
  openai: { configured: false },
  ollama: { configured: true, reachable: false }
}

const modelsResponse: ModelListResponse = {
  models: [
    {
      model_id: 'anthropic.claude-3-sonnet',
      name: 'Claude 3 Sonnet',
      provider: 'Anthropic',
      supports_streaming: true,
      kind: 'foundation-model',
      source: 'bedrock'
    }
  ],
  providers: modelProviders,
  cached: true
}

beforeEach(() => {
  modelsListMock.mockReset()
  useModelStore.getState().clear()
})

describe('loadModels', () => {
  it('starts from INITIAL_MODEL_STATE', () => {
    expect(useModelStore.getState()).toMatchObject(INITIAL_MODEL_STATE)
  })

  it('hits the API once for two concurrent calls', async () => {
    modelsListMock.mockResolvedValue(modelsResponse)

    await Promise.all([
      useModelStore.getState().loadModels(),
      useModelStore.getState().loadModels()
    ])

    expect(modelsListMock).toHaveBeenCalledTimes(1)
    const state = useModelStore.getState()
    expect(state.models).toEqual(modelsResponse.models)
    expect(state.modelsCached).toBe(true)
    expect(state.modelsLoaded).toBe(true)
    expect(state.modelProviders).toEqual(modelProviders)
  })

  it('does not refetch once loaded, unless forced', async () => {
    modelsListMock.mockResolvedValue(modelsResponse)

    await useModelStore.getState().loadModels()
    await useModelStore.getState().loadModels()
    expect(modelsListMock).toHaveBeenCalledTimes(1)

    await useModelStore.getState().loadModels(true)
    expect(modelsListMock).toHaveBeenCalledTimes(2)
  })

  it('findModel looks a row up by model_id', async () => {
    modelsListMock.mockResolvedValue(modelsResponse)
    await useModelStore.getState().loadModels()

    const models = useModelStore.getState().models
    expect(findModel(models, 'anthropic.claude-3-sonnet')?.name).toBe('Claude 3 Sonnet')
    expect(findModel(models, 'nope')).toBeNull()
  })

  it('tolerates an abort, clearing loading without recording an error', async () => {
    modelsListMock.mockRejectedValueOnce(new StreamAbortedError())

    await useModelStore.getState().loadModels()

    expect(useModelStore.getState().modelsLoading).toBe(false)
    expect(useModelStore.getState().modelsError).toBeNull()
    expect(useModelStore.getState().modelsLoaded).toBe(false)
  })

  it('records a real loadModels failure as modelsError', async () => {
    modelsListMock.mockRejectedValueOnce(new ApiError('down', { code: 'http_error' }))

    await useModelStore.getState().loadModels()

    expect(useModelStore.getState().modelsError).toEqual({ code: 'http_error', message: 'down' })
    expect(useModelStore.getState().modelsLoading).toBe(false)
  })

  it('retries on a later call after a failed fetch, rather than replaying the stale in-flight request', async () => {
    modelsListMock.mockRejectedValueOnce(new ApiError('down', { code: 'http_error' }))
    await useModelStore.getState().loadModels()
    expect(modelsListMock).toHaveBeenCalledTimes(1)

    modelsListMock.mockResolvedValueOnce(modelsResponse)
    await useModelStore.getState().loadModels()

    expect(modelsListMock).toHaveBeenCalledTimes(2)
    expect(useModelStore.getState().modelsLoaded).toBe(true)
  })

  it('sets modelsLoading:true synchronously, before the request resolves', async () => {
    let resolveModels!: (r: ModelListResponse) => void
    modelsListMock.mockImplementationOnce(
      () =>
        new Promise<ModelListResponse>(resolve => {
          resolveModels = resolve
        })
    )

    const pending = useModelStore.getState().loadModels()
    expect(useModelStore.getState().modelsLoading).toBe(true)

    resolveModels(modelsResponse)
    await pending
    expect(useModelStore.getState().modelsLoading).toBe(false)
  })

  it('clear() drops the in-flight guard so the next load refetches', async () => {
    modelsListMock.mockResolvedValue(modelsResponse)
    await useModelStore.getState().loadModels()

    useModelStore.getState().clear()
    expect(useModelStore.getState().models).toEqual([])

    await useModelStore.getState().loadModels()
    expect(modelsListMock).toHaveBeenCalledTimes(2)
  })
})

describe('groupModelsBySource', () => {
  it('reports nothing at all before the catalog (and its providers) has loaded', () => {
    expect(groupModelsBySource([], null)).toEqual({ groups: [], unavailable: [] })
  })

  const models: ModelInfo[] = [
    {
      model_id: 'm-bedrock',
      name: 'Bedrock model',
      provider: 'Amazon',
      supports_streaming: true,
      kind: 'foundation-model',
      source: 'bedrock'
    },
    {
      model_id: 'm-anthropic',
      name: 'Anthropic model',
      provider: 'Anthropic',
      supports_streaming: true,
      kind: 'foundation-model',
      source: 'anthropic'
    },
    {
      model_id: 'm-ollama',
      name: 'Ollama model',
      provider: 'Ollama',
      supports_streaming: false,
      kind: 'foundation-model',
      source: 'ollama'
    }
  ]

  it('groups in a fixed Bedrock/Anthropic/OpenAI/Ollama order, only for sources with rows', () => {
    const { groups } = groupModelsBySource(models, {
      bedrock: { configured: true },
      anthropic: { configured: true },
      openai: { configured: true },
      ollama: { configured: true, reachable: true }
    })

    expect(groups.map(g => g.source)).toEqual(['bedrock', 'anthropic', 'ollama'])
    expect(groups[0].models.map(m => m.model_id)).toEqual(['m-bedrock'])
    expect(groups.some(g => g.disabled)).toBe(false)
  })

  it('disables and suffixes an unconfigured source', () => {
    const { groups } = groupModelsBySource(models, {
      bedrock: { configured: true },
      anthropic: { configured: false },
      openai: { configured: true },
      ollama: { configured: true, reachable: true }
    })

    const anthropicGroup = groups.find(g => g.source === 'anthropic')
    expect(anthropicGroup?.disabled).toBe(true)
    expect(anthropicGroup?.label).toBe('Anthropic (not configured)')
  })

  it('disables and suffixes a configured-but-unreachable ollama, without affecting other configured sources', () => {
    const { groups } = groupModelsBySource(models, {
      bedrock: { configured: true },
      anthropic: { configured: true },
      openai: { configured: true },
      ollama: { configured: true, reachable: false }
    })

    const ollamaGroup = groups.find(g => g.source === 'ollama')
    expect(ollamaGroup?.disabled).toBe(true)
    expect(ollamaGroup?.label).toBe('Ollama (local) (unreachable)')

    // Ollama's own unreachable flag must not leak into unrelated sources.
    const bedrockGroup = groups.find(g => g.source === 'bedrock')
    expect(bedrockGroup?.disabled).toBe(false)
    expect(bedrockGroup?.label).toBe('Bedrock')
    const anthropicGroup = groups.find(g => g.source === 'anthropic')
    expect(anthropicGroup?.disabled).toBe(false)
    expect(anthropicGroup?.label).toBe('Anthropic')
  })

  it('does not disable ollama when reachable is unknown (null)', () => {
    const { groups } = groupModelsBySource(models, {
      bedrock: { configured: true },
      anthropic: { configured: true },
      openai: { configured: true },
      ollama: { configured: true, reachable: null }
    })

    const ollamaGroup = groups.find(g => g.source === 'ollama')
    expect(ollamaGroup?.disabled).toBe(false)
    expect(ollamaGroup?.label).toBe('Ollama (local)')
  })

  it('footnotes an unconfigured source that has no catalog rows at all', () => {
    const { groups, unavailable } = groupModelsBySource(models, {
      bedrock: { configured: true },
      anthropic: { configured: true },
      openai: { configured: false },
      ollama: { configured: false, reachable: null }
    })

    expect(groups.some(g => g.source === 'openai')).toBe(false)
    expect(unavailable).toEqual([{ source: 'openai', label: 'OpenAI' }])
  })
})
