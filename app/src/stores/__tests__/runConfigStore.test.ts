/** runConfigStore: persistence round-trip, toolset selection, request building. */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, api: { ...actual.api } }
})

const {
  useRunConfigStore,
  toRunRequest,
  selectCanRun,
  DEFAULT_RUN_CONFIG,
  RUN_CONFIG_STORAGE_KEY
} = await import('../runConfigStore')

beforeEach(() => {
  localStorage.clear()
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
})

describe('defaults', () => {
  it('starts from DEFAULT_RUN_CONFIG with streaming on and no toolset', () => {
    const state = useRunConfigStore.getState()
    expect(state.model_id).toBe('')
    expect(state.provider).toBe('bedrock')
    expect(state.stream).toBe(true)
    expect(state.toolset).toBeNull()
    expect(state.max_tool_iterations).toBe(10)
    expect(state.guardrail).toBeNull()
    expect(state.inference).toEqual({})
  })

  it('selectCanRun needs a model and a user prompt', () => {
    expect(selectCanRun(useRunConfigStore.getState())).toBe(false)
    useRunConfigStore.getState().setModelId('anthropic.claude-3-sonnet')
    expect(selectCanRun(useRunConfigStore.getState())).toBe(false)
    useRunConfigStore.getState().setUserPrompt('hello')
    expect(selectCanRun(useRunConfigStore.getState())).toBe(true)
  })

  it('selectCanRun treats a whitespace-only model id or user prompt as unset', () => {
    useRunConfigStore.getState().setModelId('   ')
    useRunConfigStore.getState().setUserPrompt('hello')
    expect(selectCanRun(useRunConfigStore.getState())).toBe(false)

    useRunConfigStore.getState().setModelId('m')
    useRunConfigStore.getState().setUserPrompt('  \t ')
    expect(selectCanRun(useRunConfigStore.getState())).toBe(false)
  })
})

describe('setters', () => {
  it('merges inference patches and deletes keys set to undefined', () => {
    const { setInference } = useRunConfigStore.getState()
    setInference({ temperature: 0.2 })
    setInference({ max_tokens: 512 })
    expect(useRunConfigStore.getState().inference).toEqual({ temperature: 0.2, max_tokens: 512 })

    setInference({ temperature: undefined })
    expect(useRunConfigStore.getState().inference).toEqual({ max_tokens: 512 })
  })

  it('setToolset stores a name and clears back to null', () => {
    useRunConfigStore.getState().setToolset('fraud-detection')
    expect(useRunConfigStore.getState().toolset).toBe('fraud-detection')

    useRunConfigStore.getState().setToolset(null)
    expect(useRunConfigStore.getState().toolset).toBeNull()
  })

  it('setSystemPrompt / setUserPrompt / setStream write their fields', () => {
    const state = useRunConfigStore.getState()
    state.setSystemPrompt('be terse')
    state.setUserPrompt('hello')
    state.setStream(false)

    const next = useRunConfigStore.getState()
    expect(next.system_prompt).toBe('be terse')
    expect(next.user_prompt).toBe('hello')
    expect(next.stream).toBe(false)
  })

  it('reset() restores the defaults', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('m')
    state.setUserPrompt('p')
    state.setToolset('fraud-detection')
    state.setGuardrail({ id: 'gr-1', version: 'DRAFT', trace: true })
    state.reset()

    const next = useRunConfigStore.getState()
    expect(next.model_id).toBe('')
    expect(next.user_prompt).toBe('')
    expect(next.toolset).toBeNull()
    expect(next.guardrail).toBeNull()
    expect(next.provider).toBe('bedrock')
  })
})

describe('provider / guardrail invariant', () => {
  it('selectModel sets model_id and provider together', () => {
    useRunConfigStore.getState().selectModel('claude-3-opus', 'anthropic')

    const state = useRunConfigStore.getState()
    expect(state.model_id).toBe('claude-3-opus')
    expect(state.provider).toBe('anthropic')
  })

  it('setProvider clears an already-selected guardrail when leaving bedrock', () => {
    useRunConfigStore.getState().setGuardrail({ id: 'gr-1', trace: true })
    expect(useRunConfigStore.getState().guardrail).not.toBeNull()

    useRunConfigStore.getState().setProvider('openai')

    const state = useRunConfigStore.getState()
    expect(state.provider).toBe('openai')
    expect(state.guardrail).toBeNull()
  })

  it('selectModel clears an already-selected guardrail when switching to a non-bedrock model', () => {
    useRunConfigStore.getState().setGuardrail({ id: 'gr-1', trace: true })

    useRunConfigStore.getState().selectModel('gpt-4o', 'openai')

    expect(useRunConfigStore.getState().guardrail).toBeNull()
  })

  it('setProvider back to bedrock does not resurrect a cleared guardrail', () => {
    useRunConfigStore.getState().setGuardrail({ id: 'gr-1', trace: true })
    useRunConfigStore.getState().setProvider('openai')
    useRunConfigStore.getState().setProvider('bedrock')

    expect(useRunConfigStore.getState().guardrail).toBeNull()
  })

  it('setProvider leaves an existing guardrail alone when staying on bedrock', () => {
    useRunConfigStore.getState().setGuardrail({ id: 'gr-1', trace: true })
    useRunConfigStore.getState().setProvider('bedrock')

    expect(useRunConfigStore.getState().guardrail).toEqual({ id: 'gr-1', trace: true })
  })
})

describe('toRunRequest', () => {
  it('omits empty optional fields but always sends toolset (null means no tools)', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('anthropic.claude-3-sonnet')
    state.setUserPrompt('hi')

    expect(toRunRequest(useRunConfigStore.getState())).toEqual({
      model_id: 'anthropic.claude-3-sonnet',
      user_prompt: 'hi',
      toolset: null,
      max_tool_iterations: 10,
      provider: 'bedrock',
      stream: true
    })
  })

  it('includes system prompt, toolset, inference and guardrail when set', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('m')
    state.setUserPrompt('hi')
    state.setSystemPrompt('be terse')
    state.setInference({ temperature: 0.1, top_p: 0.9 })
    state.setGuardrail({ id: 'gr-1', version: '2', trace: true })
    state.setToolset('fraud-detection')
    state.setMaxToolIterations(4)
    state.setStream(false)

    expect(toRunRequest(useRunConfigStore.getState())).toEqual({
      model_id: 'm',
      user_prompt: 'hi',
      system_prompt: 'be terse',
      inference: { temperature: 0.1, top_p: 0.9 },
      guardrail: { id: 'gr-1', version: '2', trace: true },
      toolset: 'fraud-detection',
      max_tool_iterations: 4,
      provider: 'bedrock',
      stream: false
    })
  })

  it('never carries the removed scenario/dataset/tools_enabled keys', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('m')
    state.setUserPrompt('hi')

    const request = toRunRequest(useRunConfigStore.getState())
    expect(request).not.toHaveProperty('scenario_id')
    expect(request).not.toHaveProperty('dataset_id')
    expect(request).not.toHaveProperty('tools_enabled')
  })

  it('omits a whitespace-only system prompt (trimmed to empty)', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('m')
    state.setUserPrompt('hi')
    state.setSystemPrompt('   \n\t  ')

    expect(toRunRequest(useRunConfigStore.getState())).not.toHaveProperty('system_prompt')
  })

  it('copies inference and guardrail rather than aliasing store state', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('m')
    state.setUserPrompt('hi')
    state.setInference({ temperature: 0.1 })
    state.setGuardrail({ id: 'gr-1', trace: true })

    const current = useRunConfigStore.getState()
    const request = toRunRequest(current)
    expect(request.inference).not.toBe(current.inference)
    expect(request.guardrail).not.toBe(current.guardrail)
  })

  it('always includes provider, even the bedrock default', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('m')
    state.setUserPrompt('hi')

    expect(toRunRequest(useRunConfigStore.getState()).provider).toBe('bedrock')

    state.setProvider('anthropic')
    expect(toRunRequest(useRunConfigStore.getState()).provider).toBe('anthropic')
  })
})

describe('persistence', () => {
  it('uses a v2 key, so a v1 payload with the old shape is never read', () => {
    expect(RUN_CONFIG_STORAGE_KEY).toBe('evalharness.run-config.v2')
  })

  it('writes every config field to localStorage under the v2 key', () => {
    const state = useRunConfigStore.getState()
    state.setModelId('anthropic.claude-3-sonnet')
    state.setSystemPrompt('be terse')
    state.setUserPrompt('where is B456?')
    state.setInference({ temperature: 0.3 })
    state.setToolset('fraud-detection')
    state.setGuardrail({ id: 'gr-1', version: 'DRAFT', trace: true })

    const raw = localStorage.getItem(RUN_CONFIG_STORAGE_KEY)
    expect(raw).not.toBeNull()

    const parsed = JSON.parse(raw as string)
    expect(parsed.version).toBe(2)
    expect(parsed.state).toEqual({
      model_id: 'anthropic.claude-3-sonnet',
      provider: 'bedrock',
      system_prompt: 'be terse',
      user_prompt: 'where is B456?',
      inference: { temperature: 0.3 },
      toolset: 'fraud-detection',
      max_tool_iterations: 10,
      guardrail: { id: 'gr-1', version: 'DRAFT', trace: true },
      stream: true
    })
    // no functions leaked into the persisted payload
    expect(Object.keys(parsed.state)).toHaveLength(9)
  })

  it('round-trips: a stored payload rehydrates back into the store', async () => {
    localStorage.setItem(
      RUN_CONFIG_STORAGE_KEY,
      JSON.stringify({
        version: 2,
        state: {
          ...DEFAULT_RUN_CONFIG,
          model_id: 'amazon.nova-pro-v1:0',
          user_prompt: 'restored prompt',
          toolset: 'fraud-detection',
          inference: { max_tokens: 256 },
          provider: 'anthropic',
          stream: false
        }
      })
    )

    await useRunConfigStore.persist.rehydrate()

    const state = useRunConfigStore.getState()
    expect(state.model_id).toBe('amazon.nova-pro-v1:0')
    expect(state.user_prompt).toBe('restored prompt')
    expect(state.toolset).toBe('fraud-detection')
    expect(state.inference).toEqual({ max_tokens: 256 })
    expect(state.provider).toBe('anthropic')
    expect(state.stream).toBe(false)
    // actions survive rehydration
    expect(typeof state.setToolset).toBe('function')
  })

  it('a payload predating a field rehydrates with that field at its default', async () => {
    // Simulates a persisted payload with no `toolset` key at all (not even
    // `undefined`), the way real old localStorage looks.
    const withoutToolset: Record<string, unknown> = { ...DEFAULT_RUN_CONFIG }
    delete withoutToolset.toolset
    localStorage.setItem(
      RUN_CONFIG_STORAGE_KEY,
      JSON.stringify({
        version: 2,
        state: {
          ...withoutToolset,
          model_id: 'amazon.nova-pro-v1:0',
          user_prompt: 'restored prompt'
        }
      })
    )

    await useRunConfigStore.persist.rehydrate()

    const state = useRunConfigStore.getState()
    expect(state.model_id).toBe('amazon.nova-pro-v1:0')
    expect(state.toolset).toBeNull()
  })
})
