/**
 * Workbench, wired to the real `runConfigStore` with only the network-facing
 * actions stubbed.
 *
 * The point of the test is the seam between the form and the run: Run stays
 * disabled until `selectCanRun` is satisfied, and pressing it hands `startRun`
 * exactly the `toRunRequest` body — optional fields omitted rather than sent as
 * empty noise.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ModelInfo, ToolsResponse, ModelProviders } from '../../../api'

const toolsMock = vi.fn<() => Promise<ToolsResponse>>()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
})

const WorkbenchPage = (await import('../WorkbenchPage')).default
const {
  DEFAULT_RUN_CONFIG,
  INITIAL_MCP_SERVER_STATE,
  INITIAL_RUN_STATE,
  useGuardrailStore,
  useMcpServerStore,
  useModelStore,
  useRunConfigStore,
  useRunStore
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

const startRun = vi.fn().mockResolvedValue(undefined)

async function renderSettled() {
  render(<WorkbenchPage />)
  await waitFor(() =>
    expect(screen.getByRole('checkbox', { name: 'fraud-detection' })).toBeInTheDocument()
  )
}

beforeEach(() => {
  startRun.mockClear()
  toolsMock.mockReset()
  toolsMock.mockResolvedValue({
    toolsets: [{ name: 'fraud-detection', tools: ['lookupAccount'] }]
  })
  useRunStore.setState({ ...INITIAL_RUN_STATE, startRun })
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useModelStore.setState({
    models: MODELS,
    modelsLoaded: true,
    modelProviders: ALL_PROVIDERS,
    loadModels: vi.fn().mockResolvedValue(undefined)
  })
  useGuardrailStore.setState({
    guardrails: [],
    loaded: true,
    loadGuardrails: vi.fn().mockResolvedValue(undefined)
  })
  useMcpServerStore.setState({
    ...INITIAL_MCP_SERVER_STATE,
    servers: [
      {
        id: 'mcp-1',
        name: 'GitHub MCP',
        url: 'https://mcp.github.example/mcp',
        header_names: ['Authorization'],
        created_at: '2026-09-01T00:00:00Z',
        updated_at: '2026-09-01T00:00:00Z'
      }
    ],
    loaded: true,
    loadServers: vi.fn().mockResolvedValue(undefined)
  })
})

describe('WorkbenchPage', () => {
  it('renders no scenario panel', async () => {
    await renderSettled()

    expect(screen.queryByRole('heading', { name: 'Scenario' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/scenario/i)).not.toBeInTheDocument()
  })

  it('keeps Run disabled until a model and a user prompt are set', async () => {
    await renderSettled()

    const runButton = screen.getByRole('button', { name: 'Run' })
    expect(runButton).toBeDisabled()

    // A model alone is not enough.
    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), {
      target: { value: MODELS[0].model_id }
    })
    expect(runButton).toBeDisabled()

    fireEvent.change(screen.getByLabelText('User prompt'), {
      target: { value: 'Summarise this transaction.' }
    })
    expect(runButton).toBeEnabled()
  })

  it('starts a run with the toRunRequest body built from the form', async () => {
    await renderSettled()

    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), {
      target: { value: MODELS[1].model_id }
    })
    fireEvent.change(screen.getByLabelText('System prompt'), {
      target: { value: 'You are a fraud analyst.' }
    })
    fireEvent.change(screen.getByLabelText('User prompt'), {
      target: { value: 'Is this suspicious?' }
    })
    fireEvent.click(screen.getByRole('checkbox', { name: 'fraud-detection' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'GitHub MCP' }))

    fireEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(startRun).toHaveBeenCalledTimes(1)
    expect(startRun).toHaveBeenCalledWith({
      model_id: 'amazon.nova-pro-v1:0',
      user_prompt: 'Is this suspicious?',
      system_prompt: 'You are a fraud analyst.',
      toolset: 'fraud-detection',
      mcp_servers: ['mcp-1'],
      max_tool_iterations: 10,
      provider: 'bedrock',
      stream: true
    })
  })

  it('sends toolset: null and no mcp_servers when no tools are picked', async () => {
    await renderSettled()

    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), {
      target: { value: MODELS[0].model_id }
    })
    fireEvent.change(screen.getByLabelText('User prompt'), { target: { value: 'go' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(startRun).toHaveBeenCalledWith(expect.objectContaining({ toolset: null }))
    expect(startRun.mock.calls[0][0]).not.toHaveProperty('mcp_servers')
  })

  it('offers Cancel only while a run is in flight', async () => {
    await renderSettled()

    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()

    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), {
      target: { value: MODELS[0].model_id }
    })
    fireEvent.change(screen.getByLabelText('User prompt'), { target: { value: 'go' } })
    act(() => useRunStore.setState({ status: 'streaming' }))

    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Running…' })).toBeDisabled()
  })
})
