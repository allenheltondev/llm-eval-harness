/**
 * Launches a `kind: "determinism"` evaluation of the *current* workbench run
 * config (read from `runConfigStore`, never duplicated into local state) plus
 * N / grader settings that are local to this form.
 *
 * `run_config` is built with `toRunRequest` at submit time — same helper the
 * Workbench's Run button uses — so an evaluation always replays exactly the
 * request a manual run would send.
 *
 * Run location follows the server: health's `cloud_evals.configured` gates the
 * Cloud option, `local_evals.available` gates "This machine". A deployment with
 * no local lane flips the *effective* choice to cloud without touching the
 * stored `defaultEvalExecution`.
 *
 * The grader model picker is grouped the same way as the Workbench's
 * `ModelPanel` (`groupModelsBySource`); picking a grader model sets its
 * provider alongside it, same idea as `runConfigStore.selectModel`. Left
 * untouched, the grader defaults stay `bedrock` / nova (`DEFAULT_GRADER_MODEL_ID`).
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  Input,
  SegmentedControl,
  Select,
  TextArea
} from '@readysetcloud/ui'
import {
  findModel,
  groupModelsBySource,
  selectCanRun,
  selectIsEvaluating,
  toRunRequest,
  useEvalStore,
  useModelStore,
  useRunConfigStore,
  useSettingsStore
} from '../../stores'
import { api } from '../../api'
import type {
  EvaluationExecution,
  EvaluationGraderConfig,
  EvaluationRequest,
  ModelSource
} from '../../api'

const N_MIN = 2
const N_MAX = 25

const CLOUD_UNAVAILABLE_TOOLTIP = 'Cloud lane not configured on the server'
const LOCAL_UNAVAILABLE_TOOLTIP = 'Local execution is unavailable on this deployment'

const EXECUTION_OPTIONS: Array<{ value: EvaluationExecution; label: string }> = [
  { value: 'local', label: 'This machine' },
  { value: 'cloud', label: 'Cloud — persisted' }
]

function clampN(value: number): number {
  if (!Number.isFinite(value)) return N_MIN
  return Math.min(N_MAX, Math.max(N_MIN, Math.round(value)))
}

/** Trim to a single-line preview; empty text renders as an em dash. */
function truncate(text: string, max = 140): string {
  const trimmed = text.trim()
  if (trimmed === '') return '—'
  return trimmed.length <= max ? trimmed : `${trimmed.slice(0, max)}…`
}

interface DeterminismLauncherProps {
  /** Called with the new evaluation's id once `POST /evaluations` succeeds. */
  onStarted?: (evaluationId: string) => void
}

export default function DeterminismLauncher({ onStarted }: DeterminismLauncherProps) {
  const modelId = useRunConfigStore(state => state.model_id)
  const toolset = useRunConfigStore(state => state.toolset)
  const systemPrompt = useRunConfigStore(state => state.system_prompt)
  const userPrompt = useRunConfigStore(state => state.user_prompt)
  const canRun = useRunConfigStore(selectCanRun)

  const models = useModelStore(state => state.models)
  const modelProviders = useModelStore(state => state.modelProviders)
  const loadModels = useModelStore(state => state.loadModels)

  const defaultGraderModelId = useSettingsStore(state => state.defaultGraderModelId)
  const defaultN = useSettingsStore(state => state.defaultN)
  const defaultEvalExecution = useSettingsStore(state => state.defaultEvalExecution)
  const setDefaultEvalExecution = useSettingsStore(state => state.setDefaultEvalExecution)

  const startEvaluation = useEvalStore(state => state.startEvaluation)
  const isEvaluating = useEvalStore(selectIsEvaluating)
  const startErrorState = useEvalStore(state => state.error)

  const [n, setN] = useState(() => clampN(defaultN))
  const [graderModelId, setGraderModelId] = useState(defaultGraderModelId)
  // Grader defaults stay bedrock/nova (`DEFAULT_GRADER_MODEL_ID`); switching
  // the grader model picker updates this alongside the id, same as the
  // Workbench's `selectModel`.
  const [graderProvider, setGraderProvider] = useState<ModelSource>('bedrock')
  const [rubric, setRubric] = useState('')
  const [graderSystemPrompt, setGraderSystemPrompt] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [startFailed, setStartFailed] = useState(false)
  const [execution, setExecution] = useState<EvaluationExecution>(defaultEvalExecution)
  // Unknown/failed health is treated as "not configured" (see module docs on
  // `api.health`): the cloud option starts — and stays — disabled unless a
  // health check comes back and says otherwise.
  const [cloudConfigured, setCloudConfigured] = useState(false)
  // The local lane goes the other way: a local-first tool assumes it can run
  // locally, and only a health response that explicitly says
  // `local_evals.available === false` (a deployed server) takes that away.
  const [localAvailable, setLocalAvailable] = useState(true)

  useEffect(() => {
    void loadModels()
  }, [loadModels])

  useEffect(() => {
    let cancelled = false
    api
      .health()
      .then(health => {
        if (cancelled) return
        setCloudConfigured(health.cloud_evals.configured)
        setLocalAvailable(health.local_evals.available)
      })
      .catch(() => {
        if (!cancelled) setCloudConfigured(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // What this launch will actually use. When the deployment has no local lane,
  // a stored `defaultEvalExecution: 'local'` is overridden for *this* form
  // rather than rewritten in settings — the preference is still the right one
  // the next time the user points the UI at their own machine.
  const effectiveExecution: EvaluationExecution =
    !localAvailable && execution === 'local' ? 'cloud' : execution

  function handleExecutionChange(next: EvaluationExecution) {
    setExecution(next)
    setDefaultEvalExecution(next)
  }

  const model = findModel(models, modelId)
  const graderReady = graderModelId.trim() !== ''
  const graderGroups = groupModelsBySource(models, modelProviders).groups

  function handleGraderModelChange(id: string) {
    setGraderModelId(id)
    const graderModel = findModel(models, id)
    setGraderProvider(graderModel?.source ?? 'bedrock')
  }

  async function handleStart() {
    setStartFailed(false)
    const grader: EvaluationGraderConfig = { model_id: graderModelId, provider: graderProvider }
    if (graderSystemPrompt.trim() !== '') grader.system_prompt = graderSystemPrompt

    const request: EvaluationRequest = {
      kind: 'determinism',
      run_config: toRunRequest(useRunConfigStore.getState()),
      n,
      grader,
      execution: effectiveExecution,
      source: 'ui'
    }
    if (rubric.trim() !== '') request.rubric = rubric

    const id = await startEvaluation(request)
    if (id) onStarted?.(id)
    else setStartFailed(true)
  }

  const unavailableHints = EXECUTION_OPTIONS.filter(option =>
    option.value === 'cloud' ? !cloudConfigured : !localAvailable
  ).map(option =>
    option.value === 'cloud' ? CLOUD_UNAVAILABLE_TOOLTIP : LOCAL_UNAVAILABLE_TOOLTIP
  )

  return (
    <Card role="region" aria-labelledby="determinism-launcher-heading">
      <CardHeader>
        <h2 id="determinism-launcher-heading" className="card-title">
          New determinism evaluation
        </h2>
      </CardHeader>
      <CardBody>
        {!canRun ? (
          <Alert variant="info" className="mb-4">
            Set a model and a user prompt in the Workbench tab before starting a determinism
            evaluation.
          </Alert>
        ) : (
          <dl
            className="text-sm text-muted-foreground space-y-1 mb-4"
            data-testid="workbench-config-summary"
          >
            <div>
              <dt className="inline font-medium text-foreground">Model: </dt>
              <dd className="inline font-mono text-xs">{model?.name ?? modelId}</dd>
            </div>
            <div>
              <dt className="inline font-medium text-foreground">Tools: </dt>
              <dd className="inline">{toolset ?? 'None'}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">System prompt</dt>
              <dd className="text-xs text-muted-foreground font-mono">{truncate(systemPrompt)}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">User prompt</dt>
              <dd className="text-xs text-muted-foreground font-mono">{truncate(userPrompt)}</dd>
            </div>
          </dl>
        )}

        <div className="space-y-4">
          <div className="field">
            <span className="field-label" aria-hidden="true">
              Run location
            </span>
            <SegmentedControl
              aria-label="Run location"
              options={EXECUTION_OPTIONS.map(option => {
                const disabled = option.value === 'cloud' ? !cloudConfigured : !localAvailable
                const hint =
                  option.value === 'cloud' ? CLOUD_UNAVAILABLE_TOOLTIP : LOCAL_UNAVAILABLE_TOOLTIP
                return {
                  value: option.value,
                  disabled,
                  label: <span title={disabled ? hint : undefined}>{option.label}</span>
                }
              })}
              value={effectiveExecution}
              onChange={handleExecutionChange}
            />
            {unavailableHints.map(hint => (
              <p key={hint} className="field-hint">
                {hint}
              </p>
            ))}
            {effectiveExecution === 'cloud' && (
              <p className="field-hint" data-testid="cloud-execution-note">
                Runs and prompts are persisted to your AWS account (DynamoDB) for later review.
              </p>
            )}
          </div>

          <Input
            label={`Number of runs (${N_MIN}–${N_MAX})`}
            type="number"
            min={N_MIN}
            max={N_MAX}
            value={n}
            onChange={event => {
              const raw = Number(event.target.value)
              if (!Number.isFinite(raw)) return
              setN(clampN(raw))
            }}
          />

          <Select
            label="Grader model"
            value={graderModelId}
            onChange={event => handleGraderModelChange(event.target.value)}
          >
            <option value="">Select a model…</option>
            {graderGroups.map(group => (
              <optgroup key={group.source} label={group.label} disabled={group.disabled}>
                {group.models.map(m => (
                  <option key={m.model_id} value={m.model_id} disabled={group.disabled}>
                    {m.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </Select>

          <TextArea
            label="Custom rubric (optional)"
            className="font-mono text-sm"
            rows={3}
            placeholder="How the judge should score consistency…"
            value={rubric}
            onChange={event => setRubric(event.target.value)}
          />

          <div>
            <Button
              variant="ghost"
              size="sm"
              aria-expanded={showAdvanced}
              aria-controls="advanced-grading-fields"
              onClick={() => setShowAdvanced(open => !open)}
            >
              {showAdvanced ? '▾' : '▸'} Advanced grading
            </Button>

            {showAdvanced && (
              <div
                id="advanced-grading-fields"
                className="mt-2 p-3 rounded-lg border border-border bg-muted/30"
              >
                <TextArea
                  label="Custom grader system prompt"
                  className="font-mono text-sm"
                  rows={4}
                  placeholder="Override the judge's default system prompt…"
                  value={graderSystemPrompt}
                  onChange={event => setGraderSystemPrompt(event.target.value)}
                />
              </div>
            )}
          </div>

          <Button
            block
            disabled={!canRun || !graderReady}
            loading={isEvaluating}
            loadingLabel="Running…"
            onClick={() => void handleStart()}
          >
            Start evaluation
          </Button>

          {startFailed && startErrorState && (
            <Alert variant="error">Could not start evaluation: {startErrorState.message}</Alert>
          )}
        </div>
      </CardBody>
    </Card>
  )
}
