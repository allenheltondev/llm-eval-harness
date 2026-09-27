/**
 * ModelPanel driven by a scripted `modelStore` — in particular the
 * error path (no AWS creds / NIMBUS_FAKE_MODEL dev runs), where the
 * catalog never loads and a model id must be typeable by hand — and the
 * multi-provider grouping (unconfigured / unreachable sources).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import ModelPanel from '../ModelPanel'
import { DEFAULT_RUN_CONFIG, useRunConfigStore, useModelStore } from '../../../stores'
import type { ModelInfo, ModelProviders } from '../../../api'

const MODELS: ModelInfo[] = [
  {
    model_id: 'amazon.nova-pro-v1:0',
    name: 'Nova Pro',
    provider: 'Amazon',
    supports_streaming: true,
    kind: 'foundation-model',
    source: 'bedrock'
  }
]

const MULTI_PROVIDER_MODELS: ModelInfo[] = [
  {
    model_id: 'amazon.nova-pro-v1:0',
    name: 'Nova Pro',
    provider: 'Amazon',
    supports_streaming: true,
    kind: 'foundation-model',
    source: 'bedrock'
  },
  {
    model_id: 'claude-opus-4',
    name: 'Claude Opus 4',
    provider: 'Anthropic',
    supports_streaming: true,
    kind: 'foundation-model',
    source: 'anthropic'
  },
  {
    model_id: 'gpt-4o',
    name: 'GPT-4o',
    provider: 'OpenAI',
    supports_streaming: true,
    kind: 'foundation-model',
    source: 'openai'
  },
  {
    model_id: 'llama3',
    name: 'Llama 3',
    provider: 'Ollama',
    supports_streaming: false,
    kind: 'foundation-model',
    source: 'ollama'
  }
]

const ALL_CONFIGURED: ModelProviders = {
  bedrock: { configured: true },
  anthropic: { configured: true },
  openai: { configured: true },
  ollama: { configured: true, reachable: true }
}

beforeEach(() => {
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useModelStore.setState({
    models: [],
    modelsLoading: false,
    modelsLoaded: false,
    modelsError: null,
    modelsCached: false,
    modelProviders: null,
    loadModels: vi.fn().mockResolvedValue(undefined)
  })
})

describe('ModelPanel', () => {
  it('renders the catalog dropdown when models load successfully', () => {
    useModelStore.setState({ models: MODELS, modelsLoaded: true })

    render(<ModelPanel />)

    expect(screen.getByRole('combobox', { name: 'Model' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Enter model id manually')).not.toBeInTheDocument()
  })

  it('offers a manual model id input when the catalog fails to load', () => {
    useModelStore.setState({
      models: [],
      modelsLoaded: false,
      modelsError: { code: 'upstream_error', message: 'Failed to list models: no credentials' }
    })

    render(<ModelPanel />)

    expect(screen.getByRole('alert')).toHaveTextContent('Could not load models')
    expect(screen.getByLabelText('Enter model id manually')).toBeInTheDocument()
  })

  it('writes a manually typed model id straight into runConfigStore', () => {
    useModelStore.setState({
      models: [],
      modelsLoaded: false,
      modelsError: { code: 'upstream_error', message: 'no credentials' }
    })

    render(<ModelPanel />)

    fireEvent.change(screen.getByLabelText('Enter model id manually'), {
      target: { value: 'fake.model-v1' }
    })

    expect(useRunConfigStore.getState().model_id).toBe('fake.model-v1')
  })

  it('offers a provider select next to the manual model id fallback, defaulting to bedrock', () => {
    useModelStore.setState({
      models: [],
      modelsLoaded: false,
      modelsError: { code: 'upstream_error', message: 'no credentials' }
    })

    render(<ModelPanel />)

    const providerSelect = screen.getByLabelText('Provider') as HTMLSelectElement
    expect(providerSelect.value).toBe('bedrock')

    fireEvent.change(providerSelect, { target: { value: 'openai' } })
    expect(useRunConfigStore.getState().provider).toBe('openai')
  })

  it('selecting a catalog model sets both model_id and provider', () => {
    useModelStore.setState({
      models: MULTI_PROVIDER_MODELS,
      modelsLoaded: true,
      modelProviders: ALL_CONFIGURED
    })

    render(<ModelPanel />)

    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), {
      target: { value: 'claude-opus-4' }
    })

    const state = useRunConfigStore.getState()
    expect(state.model_id).toBe('claude-opus-4')
    expect(state.provider).toBe('anthropic')
  })

  it('clearing the selection back to "" writes an empty model_id without touching provider', () => {
    useModelStore.setState({
      models: MULTI_PROVIDER_MODELS,
      modelsLoaded: true,
      modelProviders: ALL_CONFIGURED
    })
    useRunConfigStore.setState({ model_id: 'claude-opus-4', provider: 'anthropic' })

    render(<ModelPanel />)

    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), {
      target: { value: '' }
    })

    const state = useRunConfigStore.getState()
    expect(state.model_id).toBe('')
    expect(state.provider).toBe('anthropic')
  })

  it('shows a "cached" badge when the catalog came from the server cache', () => {
    useModelStore.setState({ models: MODELS, modelsLoaded: true, modelsCached: true })
    render(<ModelPanel />)
    expect(screen.getByText('cached')).toBeInTheDocument()
    expect(screen.getByText('cached')).toHaveAttribute(
      'title',
      "Served from the server's catalog cache"
    )
  })

  it('shows no "cached" badge when the catalog was freshly fetched', () => {
    useModelStore.setState({ models: MODELS, modelsLoaded: true, modelsCached: false })
    render(<ModelPanel />)
    expect(screen.queryByText('cached')).not.toBeInTheDocument()
  })

  it('groups options by source with Bedrock / Anthropic / OpenAI / Ollama (local) labels', () => {
    useModelStore.setState({
      models: MULTI_PROVIDER_MODELS,
      modelsLoaded: true,
      modelProviders: ALL_CONFIGURED
    })

    render(<ModelPanel />)

    const select = screen.getByRole('combobox', { name: 'Model' })
    const groupLabels = within(select)
      .getAllByRole('group')
      .map(group => group.getAttribute('label'))

    expect(groupLabels).toEqual(['Bedrock', 'Anthropic', 'OpenAI', 'Ollama (local)'])
  })

  it('marks an unconfigured provider group as disabled with a "(not configured)" suffix', () => {
    useModelStore.setState({
      models: MULTI_PROVIDER_MODELS,
      modelsLoaded: true,
      modelProviders: {
        bedrock: { configured: true },
        anthropic: { configured: false },
        openai: { configured: true },
        ollama: { configured: true, reachable: true }
      }
    })

    render(<ModelPanel />)

    const select = screen.getByRole('combobox', { name: 'Model' })
    const anthropicGroup = within(select)
      .getAllByRole('group')
      .find(group => group.getAttribute('label')?.startsWith('Anthropic'))

    expect(anthropicGroup).toHaveAttribute('label', 'Anthropic (not configured)')
    expect(anthropicGroup).toBeDisabled()
  })

  it('marks a configured-but-unreachable ollama group with "(unreachable)"', () => {
    useModelStore.setState({
      models: MULTI_PROVIDER_MODELS,
      modelsLoaded: true,
      modelProviders: {
        bedrock: { configured: true },
        anthropic: { configured: true },
        openai: { configured: true },
        ollama: { configured: true, reachable: false }
      }
    })

    render(<ModelPanel />)

    const select = screen.getByRole('combobox', { name: 'Model' })
    const ollamaGroup = within(select)
      .getAllByRole('group')
      .find(group => group.getAttribute('label')?.startsWith('Ollama'))

    expect(ollamaGroup).toHaveAttribute('label', 'Ollama (local) (unreachable)')
    expect(ollamaGroup).toBeDisabled()
  })

  it('footnotes providers with no catalog rows that are also unconfigured', () => {
    // Only bedrock models come back — no openai/ollama rows at all — and the
    // providers block says those two are unconfigured.
    useModelStore.setState({
      models: MODELS,
      modelsLoaded: true,
      modelProviders: {
        bedrock: { configured: true },
        anthropic: { configured: true },
        openai: { configured: false },
        ollama: { configured: false, reachable: null }
      }
    })

    render(<ModelPanel />)

    expect(screen.getByText(/Not shown:/)).toHaveTextContent(
      'Not shown: OpenAI (not configured), Ollama (local) (not configured)'
    )
  })

  it('keeps a persisted model id the catalog does not list visible and editable', () => {
    // Typed by hand while the catalog was down, persisted, and the catalog has
    // since recovered with other models: the select must not fall back to its
    // placeholder while Start would still send the hidden id.
    useRunConfigStore.setState({ model_id: 'my.custom-model', provider: 'anthropic' })
    useModelStore.setState({
      models: MODELS,
      modelsLoaded: true,
      modelProviders: ALL_CONFIGURED
    })

    render(<ModelPanel />)

    const select = screen.getByRole('combobox', { name: 'Model' })
    expect(select).toHaveValue('my.custom-model')
    expect(
      within(select).getByRole('option', { name: 'my.custom-model (entered manually)' })
    ).toBeInTheDocument()
    expect(screen.getByText(/is not in the model list/)).toHaveTextContent(
      'my.custom-model is not in the model list; it runs as entered, with the provider below.'
    )
    expect(screen.getByLabelText('Enter model id manually')).toHaveValue('my.custom-model')
    expect(screen.getByLabelText('Provider')).toHaveValue('anthropic')

    // Picking a listed model replaces it, and the manual row goes away.
    fireEvent.change(select, { target: { value: 'amazon.nova-pro-v1:0' } })
    expect(useRunConfigStore.getState()).toMatchObject({
      model_id: 'amazon.nova-pro-v1:0',
      provider: 'bedrock'
    })
    expect(screen.queryByLabelText('Enter model id manually')).not.toBeInTheDocument()
    expect(within(select).queryByRole('option', { name: /entered manually/ })).toBeNull()
  })

  it('does not call a persisted model unlisted while the catalog is still loading', () => {
    // A valid catalog model, persisted from last time, with the fetch in flight:
    // `models` is still empty, but nothing is known to be missing yet.
    useRunConfigStore.setState({ model_id: 'amazon.nova-pro-v1:0', provider: 'bedrock' })
    useModelStore.setState({ models: [], modelsLoading: true, modelsLoaded: false })

    const { rerender } = render(<ModelPanel />)

    const select = screen.getByRole('combobox', { name: 'Model' })
    expect(within(select).queryByRole('option', { name: /entered manually/ })).toBeNull()
    expect(screen.queryByText(/is not in the model list/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Enter model id manually')).not.toBeInTheDocument()

    // The catalog arrives with that model: it is simply selected.
    useModelStore.setState({
      models: MODELS,
      modelsLoading: false,
      modelsLoaded: true,
      modelProviders: ALL_CONFIGURED
    })
    rerender(<ModelPanel />)
    expect(select).toHaveValue('amazon.nova-pro-v1:0')
    expect(screen.queryByText(/is not in the model list/)).not.toBeInTheDocument()
  })

  it('shows an unlisted persisted id alongside the error when the catalog fails', () => {
    useRunConfigStore.setState({ model_id: 'my.custom-model', provider: 'openai' })
    useModelStore.setState({
      models: [],
      modelsLoading: false,
      modelsLoaded: false,
      modelsError: { message: 'boom', code: 'upstream_error' }
    })

    render(<ModelPanel />)

    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('my.custom-model')
    expect(screen.getByText(/Could not load models/)).toBeInTheDocument()
    expect(screen.getByLabelText('Enter model id manually')).toHaveValue('my.custom-model')
  })

  it('does not offer the manual row while the catalog is still loading', () => {
    useModelStore.setState({ models: [], modelsLoading: true })

    render(<ModelPanel />)

    expect(screen.queryByLabelText('Enter model id manually')).not.toBeInTheDocument()
    expect(screen.queryByText(/No models available/)).not.toBeInTheDocument()
  })
})
