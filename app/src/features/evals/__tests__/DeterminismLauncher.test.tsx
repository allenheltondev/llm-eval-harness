/**
 * The launcher, wired to real `runConfigStore` / `modelStore` /
 * `settingsStore` with only `evalStore.startEvaluation` stubbed.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { HealthResponse, ModelInfo, ModelProviders } from '../../../api'

/**
 * The launcher fetches `api.health()` on mount to decide which run locations
 * are selectable. Defaults to "cloud configured, local available" so most tests
 * (which don't care about the toggle) don't need to wait on it; the
 * disabled-lane tests override with `mockResolvedValueOnce`/`mockRejectedValueOnce`.
 */
const healthMock = vi.fn<() => Promise<HealthResponse>>()

function health(configured: boolean, localAvailable = true): HealthResponse {
  return {
    status: 'ok',
    aws: { region: 'us-east-1', credentials: 'ok' },
    cloud_evals: { configured },
    local_evals: { available: localAvailable },
    auth: { required: false }
  }
}

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return { ...actual, api: { ...actual.api, health: healthMock } }
})

const DeterminismLauncher = (await import('../DeterminismLauncher')).default
const {
  DEFAULT_RUN_CONFIG,
  DEFAULT_SETTINGS,
  INITIAL_EVAL_STATE,
  useEvalStore,
  useModelStore,
  useRunConfigStore,
  useSettingsStore
} = await import('../../../stores')

const ALL_PROVIDERS: ModelProviders = {
  bedrock: { configured: true },
  anthropic: { configured: true },
  openai: { configured: true },
  ollama: { configured: true, reachable: true }
}

const MODELS: ModelInfo[] = [
  {
    model_id: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    name: 'Claude 3.5 Sonnet',
    provider: 'Anthropic',
    supports_streaming: true,
    kind: 'foundation-model',
    source: 'bedrock'
  },
  {
    model_id: 'amazon.nova-pro-v1:0',
    name: 'Nova Pro',
    provider: 'Amazon',
    supports_streaming: true,
    kind: 'foundation-model',
    source: 'bedrock'
  }
]

/** A Run location option: a SegmentedControl button, pressed when selected. */
function option(name: string): HTMLElement {
  return within(screen.getByRole('group', { name: 'Run location' })).getByRole('button', { name })
}

const startEvaluation = vi.fn().mockResolvedValue('eval-new')

beforeEach(() => {
  startEvaluation.mockClear()
  startEvaluation.mockResolvedValue('eval-new')
  healthMock.mockReset()
  healthMock.mockResolvedValue(health(true))
  useEvalStore.setState({ ...INITIAL_EVAL_STATE, startEvaluation })
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useSettingsStore.setState({ ...DEFAULT_SETTINGS })
  useModelStore.setState({
    models: MODELS,
    modelsLoaded: true,
    modelProviders: ALL_PROVIDERS,
    loadModels: vi.fn().mockResolvedValue(undefined)
  })
})

describe('DeterminismLauncher', () => {
  it('disables Start until the workbench config is valid and shows a hint', async () => {
    render(<DeterminismLauncher />)
    await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

    expect(
      screen.getByText(/Set a model and a user prompt in the Workbench tab/i)
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start evaluation' })).toBeDisabled()

    act(() =>
      useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'Summarise this.' })
    )

    expect(screen.getByRole('button', { name: 'Start evaluation' })).toBeEnabled()
  })

  it('clamps N to the 2-25 range', async () => {
    useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'go' })
    render(<DeterminismLauncher />)
    await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

    const nInput = screen.getByLabelText(/Number of runs/i) as HTMLInputElement
    expect(nInput.value).toBe(String(DEFAULT_SETTINGS.defaultN))

    fireEvent.change(nInput, { target: { value: '1' } })
    expect(nInput.value).toBe('2')

    fireEvent.change(nInput, { target: { value: '999' } })
    expect(nInput.value).toBe('25')

    fireEvent.change(nInput, { target: { value: '12' } })
    expect(nInput.value).toBe('12')
  })

  it('starts an evaluation with the exact request, including rubric and grader prompt when filled', async () => {
    useRunConfigStore.setState({
      model_id: MODELS[1].model_id,
      system_prompt: 'You are a fraud analyst.',
      user_prompt: 'Is this suspicious?',
      toolset: 'fraud-detection'
    })
    render(<DeterminismLauncher />)
    await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByLabelText(/Number of runs/i), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('Grader model'), {
      target: { value: MODELS[0].model_id }
    })
    fireEvent.change(screen.getByLabelText(/Custom rubric/i), {
      target: { value: 'Penalize inconsistent tool use.' }
    })

    fireEvent.click(screen.getByRole('button', { name: /Advanced grading/i }))
    fireEvent.change(screen.getByLabelText('Custom grader system prompt'), {
      target: { value: 'You are a strict judge.' }
    })

    fireEvent.click(screen.getByRole('button', { name: 'Start evaluation' }))

    expect(startEvaluation).toHaveBeenCalledTimes(1)
    expect(startEvaluation).toHaveBeenCalledWith({
      kind: 'determinism',
      run_config: {
        model_id: MODELS[1].model_id,
        user_prompt: 'Is this suspicious?',
        system_prompt: 'You are a fraud analyst.',
        toolset: 'fraud-detection',
        max_tool_iterations: 10,
        provider: 'bedrock',
        stream: true
      },
      n: 5,
      grader: {
        model_id: MODELS[0].model_id,
        provider: 'bedrock',
        system_prompt: 'You are a strict judge.'
      },
      rubric: 'Penalize inconsistent tool use.',
      execution: 'local',
      // Recorded with the evaluation so its history can say where it started.
      source: 'ui'
    })
  })

  it('omits rubric and grader system prompt from the request when left blank', async () => {
    useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'go' })
    render(<DeterminismLauncher />)
    await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('button', { name: 'Start evaluation' }))

    expect(startEvaluation).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'determinism',
        grader: { model_id: DEFAULT_SETTINGS.defaultGraderModelId, provider: 'bedrock' }
      })
    )
    const request = startEvaluation.mock.calls[0][0]
    expect(request.rubric).toBeUndefined()
    expect(request.grader.system_prompt).toBeUndefined()
  })

  it('sends the grader provider matching the chosen grader model source', async () => {
    useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'go' })
    useModelStore.setState({
      modelProviders: ALL_PROVIDERS,
      models: [
        ...MODELS,
        {
          model_id: 'gpt-4o',
          name: 'GPT-4o',
          provider: 'OpenAI',
          supports_streaming: true,
          kind: 'foundation-model',
          source: 'openai'
        }
      ],
      modelsLoaded: true
    })
    render(<DeterminismLauncher />)
    await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByLabelText('Grader model'), { target: { value: 'gpt-4o' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start evaluation' }))

    expect(startEvaluation).toHaveBeenCalledWith(
      expect.objectContaining({
        grader: expect.objectContaining({ model_id: 'gpt-4o', provider: 'openai' })
      })
    )
  })

  it('calls onStarted with the new evaluation id', async () => {
    useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'go' })
    const onStarted = vi.fn()
    render(<DeterminismLauncher onStarted={onStarted} />)

    fireEvent.click(screen.getByRole('button', { name: 'Start evaluation' }))

    await waitFor(() => expect(onStarted).toHaveBeenCalledWith('eval-new'))
  })

  describe('Run location toggle', () => {
    it('renders both options, defaulting to "This machine"', async () => {
      render(<DeterminismLauncher />)
      await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

      expect(option('This machine')).toHaveAttribute('aria-pressed', 'true')
      expect(option('Cloud — persisted')).toHaveAttribute('aria-pressed', 'false')
    })

    it('seeds the toggle from settings.defaultEvalExecution', async () => {
      useSettingsStore.setState({ ...DEFAULT_SETTINGS, defaultEvalExecution: 'cloud' })
      render(<DeterminismLauncher />)
      await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

      expect(option('Cloud — persisted')).toHaveAttribute('aria-pressed', 'true')
    })

    it('switching to Cloud persists it as the new default and shows the storage note', async () => {
      render(<DeterminismLauncher />)
      await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

      expect(screen.queryByTestId('cloud-execution-note')).not.toBeInTheDocument()

      fireEvent.click(option('Cloud — persisted'))

      expect(option('Cloud — persisted')).toHaveAttribute('aria-pressed', 'true')
      expect(useSettingsStore.getState().defaultEvalExecution).toBe('cloud')
      expect(screen.getByTestId('cloud-execution-note')).toHaveTextContent(
        'Runs and prompts are persisted to your AWS account (DynamoDB) for later review.'
      )
    })

    it('disables the Cloud option with a tooltip when health reports the lane unconfigured', async () => {
      healthMock.mockReset()
      healthMock.mockResolvedValueOnce(health(false))
      render(<DeterminismLauncher />)

      await waitFor(() => expect(option('Cloud — persisted')).toBeDisabled())
      expect(option('Cloud — persisted').querySelector('[title]')).toHaveAttribute(
        'title',
        'Cloud lane not configured on the server'
      )
      expect(screen.getByText('Cloud lane not configured on the server')).toBeInTheDocument()
    })

    it('treats a health-check failure as unconfigured (disabled, no crash)', async () => {
      healthMock.mockReset()
      healthMock.mockRejectedValueOnce(new Error('network down'))
      render(<DeterminismLauncher />)

      await waitFor(() => expect(option('Cloud — persisted')).toBeDisabled())
    })

    it('carries execution:"cloud" on the launch request when Cloud is selected', async () => {
      useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'go' })
      render(<DeterminismLauncher />)
      await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

      fireEvent.click(option('Cloud — persisted'))
      fireEvent.click(screen.getByRole('button', { name: 'Start evaluation' }))

      expect(startEvaluation).toHaveBeenCalledWith(expect.objectContaining({ execution: 'cloud' }))
    })

    describe('when the deployment has no local lane', () => {
      beforeEach(() => {
        healthMock.mockReset()
        healthMock.mockResolvedValue(health(true, false))
      })

      it('disables "This machine" with a hint', async () => {
        render(<DeterminismLauncher />)

        await waitFor(() => expect(option('This machine')).toBeDisabled())
        expect(option('This machine').querySelector('[title]')).toHaveAttribute(
          'title',
          'Local execution is unavailable on this deployment'
        )
        expect(
          screen.getByText('Local execution is unavailable on this deployment')
        ).toBeInTheDocument()
        // The cloud lane is configured here, so it stays selectable.
        expect(option('Cloud — persisted')).toBeEnabled()
      })

      it('makes cloud the effective default without rewriting the stored preference', async () => {
        render(<DeterminismLauncher />)

        await waitFor(() =>
          expect(option('Cloud — persisted')).toHaveAttribute('aria-pressed', 'true')
        )
        expect(option('This machine')).toHaveAttribute('aria-pressed', 'false')
        expect(screen.getByTestId('cloud-execution-note')).toBeInTheDocument()
        // The user's own preference is untouched: it is right again the moment
        // they point the UI at their own machine.
        expect(useSettingsStore.getState().defaultEvalExecution).toBe(
          DEFAULT_SETTINGS.defaultEvalExecution
        )
      })

      it('launches with execution:"cloud"', async () => {
        useRunConfigStore.setState({ model_id: MODELS[0].model_id, user_prompt: 'go' })
        render(<DeterminismLauncher />)
        await waitFor(() => expect(healthMock).toHaveBeenCalledTimes(1))

        fireEvent.click(screen.getByRole('button', { name: 'Start evaluation' }))

        expect(startEvaluation).toHaveBeenCalledWith(
          expect.objectContaining({ execution: 'cloud' })
        )
      })
    })
  })
})
