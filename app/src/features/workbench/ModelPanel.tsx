/**
 * Model picker.
 *
 * Reads the catalog from `modelStore` (one idempotent `loadModels()` on
 * mount, so mounting this twice still issues a single request) and writes the
 * choice straight into `runConfigStore.model_id` / `.provider`.
 *
 * Options are grouped by `ModelInfo.source` (Bedrock / Anthropic / OpenAI /
 * Ollama (local)) rather than the free-text `provider` display string, so an
 * unconfigured or unreachable backend can be flagged consistently regardless
 * of what its models happen to be named. See `groupModelsBySource`.
 */

import { useEffect, useMemo } from 'react'
import { Alert, Badge, Card, CardBody, CardHeader, Field, Input, Select } from '@readysetcloud/ui'
import { findModel, groupModelsBySource, useRunConfigStore, useModelStore } from '../../stores'
import type { ModelSource } from '../../api'

const PROVIDER_OPTIONS: Array<{ value: ModelSource; label: string }> = [
  { value: 'bedrock', label: 'Bedrock' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'ollama', label: 'Ollama' }
]

export default function ModelPanel() {
  const models = useModelStore(state => state.models)
  const loading = useModelStore(state => state.modelsLoading)
  const error = useModelStore(state => state.modelsError)
  const cached = useModelStore(state => state.modelsCached)
  const providers = useModelStore(state => state.modelProviders)
  const loadModels = useModelStore(state => state.loadModels)

  const modelId = useRunConfigStore(state => state.model_id)
  const provider = useRunConfigStore(state => state.provider)
  const setModelId = useRunConfigStore(state => state.setModelId)
  const setProvider = useRunConfigStore(state => state.setProvider)
  const selectModel = useRunConfigStore(state => state.selectModel)

  useEffect(() => {
    void loadModels()
  }, [loadModels])

  const { groups, unavailable } = useMemo(
    () => groupModelsBySource(models, providers),
    [models, providers]
  )
  const selected = findModel(models, modelId)

  function handleSelect(id: string) {
    const model = id === '' ? null : findModel(models, id)
    if (model) selectModel(model.model_id, model.source)
    else setModelId(id)
  }

  return (
    <Card role="region" aria-labelledby="model-panel-heading">
      <CardHeader className="flex items-center justify-between">
        <h2 id="model-panel-heading" className="card-title">
          Model
        </h2>
        {cached && (
          <Badge variant="neutral" title="Served from the server's catalog cache">
            cached
          </Badge>
        )}
      </CardHeader>
      <CardBody className="space-y-2">
        {/* The card heading already reads "Model", so the select's own label
            is screen-reader only. */}
        <Field label={<span className="sr-only">Model</span>}>
          {field => (
            <select
              {...field}
              className="input"
              value={modelId}
              onChange={event => handleSelect(event.target.value)}
            >
              <option value="">
                {loading && models.length === 0 ? 'Loading models…' : 'Select a model…'}
              </option>
              {groups.map(group => (
                <optgroup key={group.source} label={group.label} disabled={group.disabled}>
                  {group.models.map(model => (
                    <option key={model.model_id} value={model.model_id} disabled={group.disabled}>
                      {model.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          )}
        </Field>

        {unavailable.length > 0 && !loading && (
          <p className="text-xs text-muted-foreground">
            Not shown: {unavailable.map(entry => `${entry.label} (not configured)`).join(', ')}
          </p>
        )}

        {selected && (
          <p className="text-xs text-muted-foreground break-all">
            <span className="font-mono">{selected.model_id}</span>
            {' · '}
            {selected.kind === 'inference-profile' ? 'inference profile' : 'foundation model'}
            {selected.supports_streaming ? ' · streaming' : ' · no streaming'}
          </p>
        )}

        {(error || (!loading && models.length === 0)) && (
          <div className="space-y-2">
            {error ? (
              <Alert variant="error">Could not load models: {error.message}</Alert>
            ) : (
              <Alert variant="info">No models available from any configured provider.</Alert>
            )}
            {/* Fallback affordance: the catalog degrades to empty rather than
                erroring when a provider listing fails (expired AWS session,
                NIMBUS_FAKE_MODEL runs, no providers configured), so let a
                model id be typed directly rather than blocking on the dropdown.
                A provider select sits next to it since a manually-typed id
                carries no `source`. */}
            <div className="flex gap-2 items-end">
              <div className="flex-1">
                <Input
                  label="Enter model id manually"
                  type="text"
                  className="font-mono text-xs"
                  placeholder="e.g. anthropic.claude-3-5-sonnet-20241022-v2:0"
                  value={modelId}
                  onChange={event => setModelId(event.target.value)}
                />
              </div>
              <div>
                <Select
                  label="Provider"
                  className="text-xs"
                  value={provider}
                  onChange={event => setProvider(event.target.value as ModelSource)}
                >
                  {PROVIDER_OPTIONS.map(option => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
          </div>
        )}
      </CardBody>
    </Card>
  )
}
