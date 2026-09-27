/**
 * RunControls: the tool picker (built-in toolsets from `GET /tools` plus saved
 * MCP servers, gating the max-iterations field; the picker's own behavior is
 * `ToolPicker.test.tsx`), the guardrail x provider invariant (guardrails only
 * run against Bedrock, so the select must be disabled off of it, and switching
 * the provider away from bedrock must clear any already-selected guardrail —
 * enforced centrally in `runConfigStore`, exercised here through the real
 * store), the inference disclosure and the Run / Cancel buttons.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { GuardrailSummary, McpServer, ToolsResponse } from '../../../api'

const toolsMock = vi.fn<() => Promise<ToolsResponse>>()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
})

const RunControls = (await import('../RunControls')).default
const {
  DEFAULT_RUN_CONFIG,
  INITIAL_MCP_SERVER_STATE,
  INITIAL_RUN_STATE,
  useGuardrailStore,
  useMcpServerStore,
  useRunConfigStore,
  useRunStore
} = await import('../../../stores')

const TOOLSETS: ToolsResponse = {
  toolsets: [
    { name: 'fraud-detection', tools: ['lookupAccount', 'flagTransaction'] },
    { name: 'empty-set', tools: [] }
  ]
}

const GUARDRAILS: GuardrailSummary[] = [
  {
    id: 'gr-1',
    arn: 'arn:aws:bedrock:us-east-1:123:guardrail/gr-1',
    name: 'PII shield',
    description: null,
    version: 'DRAFT',
    status: 'READY',
    createdAt: '2026-08-01T00:00:00Z',
    updatedAt: null
  }
]

const MCP_SERVERS: McpServer[] = [
  {
    id: 'mcp-1',
    name: 'GitHub MCP',
    url: 'https://mcp.github.example/mcp',
    header_names: ['Authorization'],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z'
  },
  {
    id: 'mcp-2',
    name: 'Docs MCP',
    url: 'http://localhost:9000/mcp',
    header_names: [],
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z'
  }
]

/** Renders and waits for the toolset fetch to settle into the picker. */
async function renderSettled() {
  const result = render(<RunControls />)
  await waitFor(() => expect(toolsMock).toHaveBeenCalledTimes(1))
  await waitFor(() =>
    expect(screen.getByRole('checkbox', { name: 'fraud-detection' })).toBeInTheDocument()
  )
  return result
}

beforeEach(() => {
  toolsMock.mockReset()
  toolsMock.mockResolvedValue(TOOLSETS)
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useRunStore.setState({ ...INITIAL_RUN_STATE, startRun: vi.fn(), cancelRun: vi.fn() })
  useGuardrailStore.setState({
    guardrails: GUARDRAILS,
    loaded: true,
    loadGuardrails: vi.fn().mockResolvedValue(undefined)
  })
  useMcpServerStore.setState({
    ...INITIAL_MCP_SERVER_STATE,
    servers: MCP_SERVERS,
    loaded: true,
    loadServers: vi.fn().mockResolvedValue(undefined)
  })
})

describe('RunControls tools', () => {
  it('fetches GET /tools once and offers one checkbox per built-in toolset and saved MCP server', async () => {
    await renderSettled()

    const group = screen.getByRole('group', { name: 'Tools' })
    expect(
      within(group)
        .getAllByRole('checkbox')
        .map(box => box.getAttribute('id'))
    ).toEqual([
      'workbench-tools-toolset-fraud-detection',
      'workbench-tools-toolset-empty-set',
      'workbench-tools-mcp-mcp-1',
      'workbench-tools-mcp-mcp-2'
    ])
    for (const box of within(group).getAllByRole('checkbox')) expect(box).not.toBeChecked()
    expect(toolsMock).toHaveBeenCalledTimes(1)
  })

  it('ticking a toolset stores its name and enables max-iterations', async () => {
    await renderSettled()

    const maxIterations = screen.getByLabelText('Max tool iterations')
    expect(maxIterations).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'fraud-detection' }))

    expect(useRunConfigStore.getState().toolset).toBe('fraud-detection')
    expect(maxIterations).toBeEnabled()
  })

  it('ticking an MCP server alone also enables max-iterations', async () => {
    await renderSettled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'GitHub MCP' }))

    expect(useRunConfigStore.getState().mcp_servers).toEqual(['mcp-1'])
    expect(screen.getByLabelText('Max tool iterations')).toBeEnabled()
  })

  it('unticking the toolset clears it back to null and disables max-iterations', async () => {
    useRunConfigStore.setState({ toolset: 'fraud-detection' })
    await renderSettled()
    expect(screen.getByLabelText('Max tool iterations')).toBeEnabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'fraud-detection' }))

    expect(useRunConfigStore.getState().toolset).toBeNull()
    expect(screen.getByLabelText('Max tool iterations')).toBeDisabled()
  })

  it('surfaces a GET /tools failure inline', async () => {
    toolsMock.mockReset()
    toolsMock.mockRejectedValue(new Error('tools unavailable'))

    render(<RunControls />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load toolsets: tools unavailable'
    )
    expect(screen.queryByRole('checkbox', { name: 'fraud-detection' })).not.toBeInTheDocument()
  })

  it('typing a max-iterations value updates the store; a non-numeric value falls back to 1', async () => {
    useRunConfigStore.setState({ toolset: 'fraud-detection' })
    await renderSettled()

    const maxIterations = screen.getByLabelText('Max tool iterations')
    fireEvent.change(maxIterations, { target: { value: '25' } })
    expect(useRunConfigStore.getState().max_tool_iterations).toBe(25)

    fireEvent.change(maxIterations, { target: { value: 'not-a-number' } })
    expect(useRunConfigStore.getState().max_tool_iterations).toBe(1)
  })
})

describe('RunControls guardrail gating', () => {
  it('leaves the guardrail select enabled on the bedrock default', async () => {
    await renderSettled()

    expect(screen.getByLabelText('Guardrail')).toBeEnabled()
    expect(screen.queryByText('Guardrails require the Bedrock provider')).not.toBeInTheDocument()
  })

  it('disables the guardrail select with a hint when the provider is not bedrock', async () => {
    useRunConfigStore.setState({ provider: 'openai' })

    await renderSettled()

    const select = screen.getByLabelText('Guardrail')
    expect(select).toBeDisabled()
    expect(select).toHaveAttribute('title', 'Guardrails require the Bedrock provider')
    expect(screen.getByText('Guardrails require the Bedrock provider')).toBeInTheDocument()
  })

  it('re-enables the guardrail select when the provider switches back to bedrock', async () => {
    useRunConfigStore.setState({ provider: 'anthropic' })
    const { rerender } = await renderSettled()
    expect(screen.getByLabelText('Guardrail')).toBeDisabled()

    act(() => useRunConfigStore.getState().setProvider('bedrock'))
    rerender(<RunControls />)

    expect(screen.getByLabelText('Guardrail')).toBeEnabled()
  })

  it('clears an already-selected guardrail when the provider switches away from bedrock', () => {
    useRunConfigStore.getState().setGuardrail({ id: 'gr-1', trace: true })
    expect(useRunConfigStore.getState().guardrail).not.toBeNull()

    useRunConfigStore.getState().setProvider('ollama')

    expect(useRunConfigStore.getState().guardrail).toBeNull()
  })

  it('selecting a guardrail from the dropdown sets it with trace on; back to "None" clears it', async () => {
    await renderSettled()

    fireEvent.change(screen.getByLabelText('Guardrail'), { target: { value: 'gr-1' } })
    expect(useRunConfigStore.getState().guardrail).toEqual({ id: 'gr-1', trace: true })

    fireEvent.change(screen.getByLabelText('Guardrail'), { target: { value: '' } })
    expect(useRunConfigStore.getState().guardrail).toBeNull()
  })
})

describe('RunControls inference parameters', () => {
  it('is collapsed by default and expands on click, toggling the disclosure caret and aria-expanded', async () => {
    await renderSettled()

    expect(screen.queryByLabelText('Temperature')).not.toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: /Inference parameters/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle.textContent).toContain('▸')

    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle.textContent).toContain('▾')
    expect(screen.getByLabelText('Temperature')).toBeInTheDocument()
    expect(screen.getByLabelText('Top P')).toBeInTheDocument()
    expect(screen.getByLabelText('Max tokens')).toBeInTheDocument()

    fireEvent.click(toggle)
    expect(screen.queryByLabelText('Temperature')).not.toBeInTheDocument()
  })

  it('editing temperature/top-p/max-tokens writes finite numbers, and blanking a field deletes the key', async () => {
    await renderSettled()
    fireEvent.click(screen.getByRole('button', { name: /Inference parameters/ }))

    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '0.7' } })
    expect(useRunConfigStore.getState().inference.temperature).toBe(0.7)

    fireEvent.change(screen.getByLabelText('Top P'), { target: { value: '0.9' } })
    expect(useRunConfigStore.getState().inference.top_p).toBe(0.9)

    fireEvent.change(screen.getByLabelText('Max tokens'), { target: { value: '512' } })
    expect(useRunConfigStore.getState().inference.max_tokens).toBe(512)

    // Blanking a field drops the key entirely rather than writing NaN/0.
    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '' } })
    expect(useRunConfigStore.getState().inference).not.toHaveProperty('temperature')
    expect(useRunConfigStore.getState().inference.top_p).toBe(0.9)
  })
})

describe('RunControls run/cancel buttons', () => {
  it('disables Run when the config cannot run, and Cancel when nothing is running', async () => {
    useRunConfigStore.setState({ model_id: '', user_prompt: '' })
    await renderSettled()

    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
  })

  it('clicking Run builds the request from the live config and calls startRun', async () => {
    const startRun = vi.fn().mockResolvedValue(undefined)
    useRunStore.setState({ startRun })
    useRunConfigStore.setState({
      model_id: 'claude-3',
      user_prompt: 'hello',
      provider: 'bedrock',
      toolset: 'fraud-detection',
      max_tool_iterations: 5
    })

    await renderSettled()
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(startRun).toHaveBeenCalledTimes(1)
    expect(startRun).toHaveBeenCalledWith(
      expect.objectContaining({
        model_id: 'claude-3',
        user_prompt: 'hello',
        provider: 'bedrock',
        toolset: 'fraud-detection',
        max_tool_iterations: 5
      })
    )
    expect(startRun.mock.calls[0][0]).not.toHaveProperty('tools_enabled')
    expect(startRun.mock.calls[0][0]).not.toHaveProperty('mcp_servers')
  })

  it('sends the ticked MCP servers, dropping ids that were deleted since', async () => {
    const startRun = vi.fn().mockResolvedValue(undefined)
    useRunStore.setState({ startRun })
    useRunConfigStore.setState({
      model_id: 'claude-3',
      user_prompt: 'hello',
      mcp_servers: ['mcp-2', 'deleted-server']
    })

    await renderSettled()
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(startRun).toHaveBeenCalledWith(expect.objectContaining({ mcp_servers: ['mcp-2'] }))
  })

  it('while running, Run is disabled and shows "Running…"; Cancel is enabled and calls cancelRun', async () => {
    const cancelRun = vi.fn()
    useRunStore.setState({ status: 'streaming', cancelRun })
    useRunConfigStore.setState({ model_id: 'claude-3', user_prompt: 'hi' })

    await renderSettled()

    const runButton = screen.getByRole('button', { name: 'Running…' })
    expect(runButton).toBeDisabled()

    const cancelButton = screen.getByRole('button', { name: 'Cancel' })
    expect(cancelButton).toBeEnabled()
    fireEvent.click(cancelButton)
    expect(cancelRun).toHaveBeenCalledTimes(1)
  })
})
