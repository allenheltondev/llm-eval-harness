/**
 * RunControls: the toolset picker (fed by `GET /tools`, gating the
 * max-iterations field), the guardrail x provider invariant (guardrails only
 * run against Bedrock, so the select must be disabled off of it, and switching
 * the provider away from bedrock must clear any already-selected guardrail —
 * enforced centrally in `runConfigStore`, exercised here through the real
 * store), the inference disclosure and the Run / Cancel buttons.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { GuardrailSummary, ToolsResponse } from '../../../api'

const toolsMock = vi.fn<() => Promise<ToolsResponse>>()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
})

const RunControls = (await import('../RunControls')).default
const { DEFAULT_RUN_CONFIG, INITIAL_RUN_STATE, useGuardrailStore, useRunConfigStore, useRunStore } =
  await import('../../../stores')

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

/** Renders and waits for the toolset fetch to settle into the select. */
async function renderSettled() {
  const result = render(<RunControls />)
  await waitFor(() => expect(toolsMock).toHaveBeenCalledTimes(1))
  await waitFor(() =>
    expect(screen.getByRole('option', { name: 'fraud-detection' })).toBeInTheDocument()
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
})

describe('RunControls tools select', () => {
  it('fetches GET /tools once and offers "None" plus one option per toolset', async () => {
    await renderSettled()

    const select = screen.getByLabelText('Tools') as HTMLSelectElement
    expect(select.value).toBe('')
    expect(Array.from(select.options).map(option => option.textContent)).toEqual([
      'None',
      'fraud-detection',
      'empty-set'
    ])
    expect(screen.queryByTestId('toolset-tools')).not.toBeInTheDocument()
  })

  it('picking a toolset stores its name, lists its tools and enables max-iterations', async () => {
    await renderSettled()

    const maxIterations = screen.getByLabelText('Max tool iterations')
    expect(maxIterations).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Tools'), { target: { value: 'fraud-detection' } })

    expect(useRunConfigStore.getState().toolset).toBe('fraud-detection')
    expect(screen.getByTestId('toolset-tools')).toHaveTextContent('lookupAccount, flagTransaction')
    expect(maxIterations).toBeEnabled()
  })

  it('an empty toolset says so instead of rendering a blank help line', async () => {
    await renderSettled()

    fireEvent.change(screen.getByLabelText('Tools'), { target: { value: 'empty-set' } })

    expect(screen.getByTestId('toolset-tools')).toHaveTextContent('No tools in this toolset')
  })

  it('choosing "None" clears the toolset back to null and disables max-iterations', async () => {
    useRunConfigStore.setState({ toolset: 'fraud-detection' })
    await renderSettled()
    expect(screen.getByLabelText('Max tool iterations')).toBeEnabled()

    fireEvent.change(screen.getByLabelText('Tools'), { target: { value: '' } })

    expect(useRunConfigStore.getState().toolset).toBeNull()
    expect(screen.getByLabelText('Max tool iterations')).toBeDisabled()
  })

  it('surfaces a GET /tools failure inline and still offers "None"', async () => {
    toolsMock.mockReset()
    toolsMock.mockRejectedValue(new Error('tools unavailable'))

    render(<RunControls />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load toolsets: tools unavailable'
    )
    const select = screen.getByLabelText('Tools') as HTMLSelectElement
    expect(Array.from(select.options).map(option => option.textContent)).toEqual(['None'])
  })

  it('reports a non-Error rejection with a generic message', async () => {
    toolsMock.mockReset()
    toolsMock.mockRejectedValue('boom')

    render(<RunControls />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load toolsets: Could not load toolsets'
    )
  })

  it('ignores a fetch that resolves after unmount', async () => {
    let resolveTools!: (value: ToolsResponse) => void
    toolsMock.mockReset()
    toolsMock.mockImplementation(
      () =>
        new Promise<ToolsResponse>(resolve => {
          resolveTools = resolve
        })
    )

    const { unmount } = render(<RunControls />)
    unmount()
    await act(async () => {
      resolveTools(TOOLSETS)
    })

    expect(screen.queryByRole('option', { name: 'fraud-detection' })).not.toBeInTheDocument()
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
