/** settingsStore: defaults, setters, persistence. */

import { beforeEach, describe, expect, it } from 'vitest'
import {
  useSettingsStore,
  resolveTheme,
  DEFAULT_SETTINGS,
  DEFAULT_EVAL_N,
  DEFAULT_GRADER_MODEL_ID,
  SETTINGS_STORAGE_KEY
} from '../settingsStore'

beforeEach(() => {
  localStorage.clear()
  useSettingsStore.setState({ ...DEFAULT_SETTINGS })
})

describe('defaults', () => {
  it('starts with system theme, nova-pro grading, n=10 and local eval execution', () => {
    const state = useSettingsStore.getState()
    expect(state.theme).toBe('system')
    expect(state.defaultGraderModelId).toBe('amazon.nova-pro-v1:0')
    expect(state.defaultN).toBe(10)
    expect(state.defaultEvalExecution).toBe('local')
  })

  it('exports the defaults as constants', () => {
    expect(DEFAULT_GRADER_MODEL_ID).toBe('amazon.nova-pro-v1:0')
    expect(DEFAULT_EVAL_N).toBe(10)
    expect(DEFAULT_SETTINGS).toEqual({
      theme: 'system',
      defaultGraderModelId: 'amazon.nova-pro-v1:0',
      defaultN: 10,
      defaultEvalExecution: 'local'
    })
  })
})

describe('setters', () => {
  it('updates each preference and resets back to the defaults', () => {
    const state = useSettingsStore.getState()
    state.setTheme('dark')
    state.setDefaultGraderModelId('anthropic.claude-3-sonnet')
    state.setDefaultN(25)
    state.setDefaultEvalExecution('cloud')

    expect(useSettingsStore.getState()).toMatchObject({
      theme: 'dark',
      defaultGraderModelId: 'anthropic.claude-3-sonnet',
      defaultN: 25,
      defaultEvalExecution: 'cloud'
    })

    useSettingsStore.getState().reset()
    expect(useSettingsStore.getState()).toMatchObject(DEFAULT_SETTINGS)
  })
})

describe('persistence', () => {
  it('writes only the preference fields under the v1 key', () => {
    useSettingsStore.getState().setTheme('light')

    const raw = localStorage.getItem(SETTINGS_STORAGE_KEY)
    expect(raw).not.toBeNull()
    const parsed = JSON.parse(raw as string)
    expect(parsed.version).toBe(1)
    expect(parsed.state).toEqual({
      theme: 'light',
      defaultGraderModelId: 'amazon.nova-pro-v1:0',
      defaultN: 10,
      defaultEvalExecution: 'local'
    })
  })

  it('rehydrates a stored payload', async () => {
    localStorage.setItem(
      SETTINGS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        state: { ...DEFAULT_SETTINGS, theme: 'dark', defaultN: 3 }
      })
    )

    await useSettingsStore.persist.rehydrate()

    expect(useSettingsStore.getState()).toMatchObject({
      theme: 'dark',
      defaultN: 3
    })
  })

  it('a pre-defaultEvalExecution payload merges cleanly, defaulting it to local', async () => {
    // Simulates a payload persisted before `defaultEvalExecution` existed: the
    // key is simply absent, not `undefined`-valued.
    const legacyState: Record<string, unknown> = {
      theme: 'dark',
      defaultGraderModelId: 'amazon.nova-pro-v1:0',
      defaultN: 10
    }
    expect('defaultEvalExecution' in legacyState).toBe(false)

    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({ version: 1, state: legacyState }))

    await useSettingsStore.persist.rehydrate()

    expect(useSettingsStore.getState().defaultEvalExecution).toBe('local')
    expect(useSettingsStore.getState().theme).toBe('dark')
  })

  it('drops the retired mascot settings from a legacy payload on the next write', async () => {
    localStorage.setItem(
      SETTINGS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        state: { ...DEFAULT_SETTINGS, robotEnabled: false, chadEnabled: false }
      })
    )

    await useSettingsStore.persist.rehydrate()
    useSettingsStore.getState().setDefaultN(4)

    const parsed = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) as string)
    expect(parsed.state).not.toHaveProperty('robotEnabled')
    expect(parsed.state).not.toHaveProperty('chadEnabled')
    expect(parsed.state.defaultN).toBe(4)
  })
})

describe('resolveTheme', () => {
  it('resolves system against the OS preference and passes explicit choices through', () => {
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme('system', false)).toBe('light')
    expect(resolveTheme('dark', false)).toBe('dark')
    expect(resolveTheme('light', true)).toBe('light')
  })
})
