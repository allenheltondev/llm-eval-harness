/**
 * Create/edit form for the simplified guardrail config.
 *
 * `guardrailId === null` is create; otherwise the DRAFT working copy is
 * hydrated (once) from `loadGuardrail` into local form state, edited freely,
 * and re-submitted whole via `updateGuardrail`. Bedrock rejects a guardrail
 * with no policy at all, so `hasAnyPolicy` mirrors that rule client-side
 * before the request is even made.
 */

import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  Input,
  Loading,
  SegmentedControl,
  Select,
  TextArea,
  type SegmentedControlOption
} from '@readysetcloud/ui'
import { useNotify } from '../../components/notify'
import { selectGuardrailDetail, useGuardrailStore } from '../../stores'
import type {
  ContentFilter,
  ContentFilterType,
  ContextualGrounding,
  DeniedTopic,
  GuardrailConfig,
  GuardrailStrength,
  ManagedWordListType,
  PiiEntity,
  PiiEntityType,
  PiiPolicy,
  WordPolicy
} from '../../api'

const CONTENT_FILTER_TYPES: ContentFilterType[] = [
  'HATE',
  'INSULTS',
  'SEXUAL',
  'VIOLENCE',
  'MISCONDUCT',
  'PROMPT_ATTACK'
]

const FILTER_LABELS: Record<ContentFilterType, string> = {
  HATE: 'Hate',
  INSULTS: 'Insults',
  SEXUAL: 'Sexual',
  VIOLENCE: 'Violence',
  MISCONDUCT: 'Misconduct',
  PROMPT_ATTACK: 'Prompt attack'
}

const STRENGTHS: GuardrailStrength[] = ['NONE', 'LOW', 'MEDIUM', 'HIGH']

/** Strength choices; every option is disabled together when the control is. */
function strengthOptions(disabled: boolean): SegmentedControlOption<GuardrailStrength>[] {
  return STRENGTHS.map(strength => ({ value: strength, label: strength, disabled }))
}

const PII_ACTION_OPTIONS: SegmentedControlOption<PiiRow['action']>[] = [
  { value: 'BLOCK', label: 'BLOCK' },
  { value: 'ANONYMIZE', label: 'ANONYMIZE' }
]

/**
 * A SegmentedControl with a visible caption styled like the package's field
 * labels. The group's accessible name comes from `aria-label` (more specific
 * than the caption, e.g. "Hate input strength"), since a <label> cannot
 * target a group of buttons.
 */
function SegmentedField<T extends string>({
  label,
  hint,
  ...control
}: {
  label: string
  hint?: string
  'aria-label': string
  options: SegmentedControlOption<T>[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div className="field">
      <span className="field-label" aria-hidden="true">
        {label}
      </span>
      <SegmentedControl {...control} />
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  )
}

const PII_ENTITY_TYPES: PiiEntityType[] = [
  'ADDRESS',
  'AGE',
  'AWS_ACCESS_KEY',
  'AWS_SECRET_KEY',
  'CA_HEALTH_NUMBER',
  'CA_SOCIAL_INSURANCE_NUMBER',
  'CREDIT_DEBIT_CARD_CVV',
  'CREDIT_DEBIT_CARD_EXPIRY',
  'CREDIT_DEBIT_CARD_NUMBER',
  'DRIVER_ID',
  'EMAIL',
  'INTERNATIONAL_BANK_ACCOUNT_NUMBER',
  'IP_ADDRESS',
  'LICENSE_PLATE',
  'MAC_ADDRESS',
  'NAME',
  'PASSWORD',
  'PHONE',
  'PIN',
  'SWIFT_CODE',
  'UK_NATIONAL_HEALTH_SERVICE_NUMBER',
  'UK_NATIONAL_INSURANCE_NUMBER',
  'UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER',
  'URL',
  'USERNAME',
  'US_BANK_ACCOUNT_NUMBER',
  'US_BANK_ROUTING_NUMBER',
  'US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER',
  'US_PASSPORT_NUMBER',
  'US_SOCIAL_SECURITY_NUMBER',
  'VEHICLE_IDENTIFICATION_NUMBER'
]

interface FilterFormState {
  enabled: boolean
  inputStrength: GuardrailStrength
  outputStrength: GuardrailStrength
}

type FilterFormMap = Record<ContentFilterType, FilterFormState>

function defaultFilterMap(): FilterFormMap {
  const map = {} as FilterFormMap
  for (const type of CONTENT_FILTER_TYPES) {
    map[type] = { enabled: false, inputStrength: 'NONE', outputStrength: 'NONE' }
  }
  return map
}

interface DeniedTopicRow {
  name: string
  definition: string
  /** Raw textarea contents; split on save. */
  examples: string
}

interface PiiRow {
  type: PiiEntityType
  action: 'BLOCK' | 'ANONYMIZE'
}

/** `"a, b\nc"` -> `["a", "b", "c"]`, capped at Bedrock's 5-example limit. */
function splitExamples(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map(entry => entry.trim())
    .filter(Boolean)
    .slice(0, 5)
}

/** `"a\nb\n\nc"` -> `["a", "b", "c"]`. */
function splitLines(raw: string): string[] {
  return raw
    .split('\n')
    .map(entry => entry.trim())
    .filter(Boolean)
}

function emptyToUndefined(value: string): string | undefined {
  const trimmed = value.trim()
  return trimmed === '' ? undefined : trimmed
}

function emptyToNull(value: string): number | null {
  const trimmed = value.trim()
  if (trimmed === '') return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

/** Bedrock rejects a guardrail with no policy configured at all. */
function hasAnyPolicy(config: GuardrailConfig): boolean {
  return Boolean(
    config.contentPolicy ||
    (config.deniedTopics && config.deniedTopics.length > 0) ||
    config.wordPolicy ||
    config.piiPolicy ||
    config.contextualGrounding
  )
}

export default function GuardrailEditor({
  guardrailId,
  onClose
}: {
  guardrailId: string | null
  onClose: () => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [blockedInputMessage, setBlockedInputMessage] = useState('')
  const [blockedOutputMessage, setBlockedOutputMessage] = useState('')
  const [filters, setFilters] = useState<FilterFormMap>(defaultFilterMap)
  const [deniedTopics, setDeniedTopics] = useState<DeniedTopicRow[]>([])
  const [words, setWords] = useState('')
  const [managedProfanity, setManagedProfanity] = useState(false)
  const [piiRows, setPiiRows] = useState<PiiRow[]>([])
  const [groundingThreshold, setGroundingThreshold] = useState('')
  const [relevanceThreshold, setRelevanceThreshold] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const notify = useNotify()

  const loadGuardrail = useGuardrailStore(state => state.loadGuardrail)
  const createGuardrail = useGuardrailStore(state => state.createGuardrail)
  const updateGuardrail = useGuardrailStore(state => state.updateGuardrail)
  const saving = useGuardrailStore(state => state.saving)
  const saveError = useGuardrailStore(state => state.saveError)
  const detail = useGuardrailStore(selectGuardrailDetail(guardrailId))
  const detailLoading = useGuardrailStore(state =>
    guardrailId ? (state.detailLoading[guardrailId] ?? false) : false
  )

  const hydratedRef = useRef(false)

  useEffect(() => {
    if (guardrailId) void loadGuardrail(guardrailId)
  }, [guardrailId, loadGuardrail])

  useEffect(() => {
    if (!detail || hydratedRef.current) return
    hydratedRef.current = true

    setName(detail.name)
    setDescription(detail.description ?? '')
    setBlockedInputMessage(detail.blockedInputMessage ?? '')
    setBlockedOutputMessage(detail.blockedOutputMessage ?? '')

    const nextFilters = defaultFilterMap()
    for (const filter of detail.contentPolicy?.filters ?? []) {
      nextFilters[filter.type] = {
        enabled: true,
        inputStrength: filter.inputStrength ?? 'NONE',
        outputStrength: filter.outputStrength ?? 'NONE'
      }
    }
    setFilters(nextFilters)

    setDeniedTopics(
      (detail.deniedTopics ?? []).map(topic => ({
        name: topic.name,
        definition: topic.definition,
        examples: (topic.examples ?? []).join('\n')
      }))
    )

    setWords((detail.wordPolicy?.words ?? []).join('\n'))
    setManagedProfanity(Boolean(detail.wordPolicy?.managedWordLists?.includes('PROFANITY')))

    setPiiRows(
      (detail.piiPolicy?.entities ?? []).map(entity => ({
        type: entity.type,
        action: entity.action === 'ANONYMIZE' ? 'ANONYMIZE' : 'BLOCK'
      }))
    )

    setGroundingThreshold(
      detail.contextualGrounding?.groundingThreshold != null
        ? String(detail.contextualGrounding.groundingThreshold)
        : ''
    )
    setRelevanceThreshold(
      detail.contextualGrounding?.relevanceThreshold != null
        ? String(detail.contextualGrounding.relevanceThreshold)
        : ''
    )
  }, [detail])

  function updateFilter(type: ContentFilterType, patch: Partial<FilterFormState>) {
    setFilters(prev => ({ ...prev, [type]: { ...prev[type], ...patch } }))
  }

  function addDeniedTopic() {
    setDeniedTopics(prev => [...prev, { name: '', definition: '', examples: '' }])
  }

  function updateDeniedTopic(index: number, patch: Partial<DeniedTopicRow>) {
    setDeniedTopics(prev => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function removeDeniedTopic(index: number) {
    setDeniedTopics(prev => prev.filter((_, i) => i !== index))
  }

  function addPiiRow() {
    setPiiRows(prev => [...prev, { type: 'EMAIL', action: 'BLOCK' }])
  }

  function updatePiiRow(index: number, patch: Partial<PiiRow>) {
    setPiiRows(prev => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function removePiiRow(index: number) {
    setPiiRows(prev => prev.filter((_, i) => i !== index))
  }

  function buildConfig(): GuardrailConfig {
    const enabledFilters: ContentFilter[] = CONTENT_FILTER_TYPES.filter(
      type => filters[type].enabled
    ).map(type => ({
      type,
      inputStrength: filters[type].inputStrength,
      outputStrength: type === 'PROMPT_ATTACK' ? 'NONE' : filters[type].outputStrength
    }))

    const cleanedDeniedTopics: DeniedTopic[] = deniedTopics
      .filter(row => row.name.trim() !== '' && row.definition.trim() !== '')
      .map(row => ({
        name: row.name.trim(),
        definition: row.definition.trim(),
        examples: splitExamples(row.examples)
      }))

    const wordList = splitLines(words)
    const managedWordLists: ManagedWordListType[] = managedProfanity ? ['PROFANITY'] : []
    const wordPolicy: WordPolicy | null =
      wordList.length > 0 || managedWordLists.length > 0
        ? { words: wordList, managedWordLists }
        : null

    const piiEntities: PiiEntity[] = piiRows.map(row => ({
      type: row.type,
      action: row.action
    }))
    const piiPolicy: PiiPolicy | null = piiEntities.length > 0 ? { entities: piiEntities } : null

    const grounding = emptyToNull(groundingThreshold)
    const relevance = emptyToNull(relevanceThreshold)
    const contextualGrounding: ContextualGrounding | null =
      grounding != null || relevance != null
        ? { groundingThreshold: grounding, relevanceThreshold: relevance }
        : null

    return {
      name: name.trim(),
      description: emptyToUndefined(description),
      contentPolicy: enabledFilters.length > 0 ? { filters: enabledFilters } : null,
      deniedTopics: cleanedDeniedTopics.length > 0 ? cleanedDeniedTopics : undefined,
      wordPolicy,
      piiPolicy,
      contextualGrounding,
      blockedInputMessage: emptyToUndefined(blockedInputMessage),
      blockedOutputMessage: emptyToUndefined(blockedOutputMessage)
    }
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setFormError(null)
    setNameError(null)

    if (name.trim() === '') {
      setNameError('Name is required.')
      return
    }

    const config = buildConfig()
    if (!hasAnyPolicy(config)) {
      setFormError(
        'Configure at least one policy — a content filter, denied topic, word list, PII rule, or grounding threshold.'
      )
      return
    }

    const result = guardrailId
      ? await updateGuardrail(guardrailId, config)
      : await createGuardrail(config)
    if (result) {
      notify(guardrailId ? 'Guardrail saved' : 'Guardrail created', { variant: 'success' })
      onClose()
    }
  }

  if (guardrailId && detailLoading && !detail) {
    return (
      <Card className="max-w-3xl mx-auto">
        <CardBody>
          <Loading text="Loading guardrail…" />
        </CardBody>
      </Card>
    )
  }

  return (
    <form
      className="max-w-3xl mx-auto space-y-4"
      data-testid="guardrail-editor"
      onSubmit={event => void handleSubmit(event)}
    >
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">
          {guardrailId ? 'Edit guardrail' : 'New guardrail'}
        </h2>
        <Button variant="secondary" onClick={onClose}>
          Back
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Basics</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          <Input
            label="Name"
            hint="Required."
            aria-required="true"
            type="text"
            value={name}
            error={nameError ?? undefined}
            onChange={event => setName(event.target.value)}
          />
          <Input
            label="Description"
            type="text"
            value={description}
            onChange={event => setDescription(event.target.value)}
          />
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <TextArea
              label="Blocked input message"
              rows={2}
              value={blockedInputMessage}
              onChange={event => setBlockedInputMessage(event.target.value)}
            />
            <TextArea
              label="Blocked output message"
              rows={2}
              value={blockedOutputMessage}
              onChange={event => setBlockedOutputMessage(event.target.value)}
            />
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Content filters</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          {CONTENT_FILTER_TYPES.map(type => {
            const filter = filters[type]
            const isPromptAttack = type === 'PROMPT_ATTACK'
            return (
              <div
                key={type}
                className="grid grid-cols-1 sm:grid-cols-[8rem_1fr_1fr] gap-3 sm:items-start p-3 rounded-lg border border-border"
                data-testid={`filter-row-${type}`}
              >
                <Field label={FILTER_LABELS[type]}>
                  {props => (
                    <input
                      {...props}
                      type="checkbox"
                      className="h-4 w-4 rounded border-border text-primary-600"
                      data-testid={`filter-enable-${type}`}
                      checked={filter.enabled}
                      onChange={event => updateFilter(type, { enabled: event.target.checked })}
                    />
                  )}
                </Field>
                <SegmentedField
                  label="Input strength"
                  aria-label={`${FILTER_LABELS[type]} input strength`}
                  options={strengthOptions(!filter.enabled)}
                  value={filter.inputStrength}
                  onChange={inputStrength => updateFilter(type, { inputStrength })}
                />
                <SegmentedField
                  label="Output strength"
                  aria-label={`${FILTER_LABELS[type]} output strength`}
                  options={strengthOptions(!filter.enabled || isPromptAttack)}
                  value={isPromptAttack ? 'NONE' : filter.outputStrength}
                  onChange={outputStrength => updateFilter(type, { outputStrength })}
                  hint={
                    isPromptAttack
                      ? 'Output strength is always NONE for prompt attacks.'
                      : undefined
                  }
                />
              </div>
            )
          })}
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Denied topics</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3" data-testid="denied-topic-rows">
          {deniedTopics.map((row, index) => (
            <div
              key={index}
              className="p-3 rounded-lg border border-border space-y-2"
              data-testid={`denied-topic-row-${index}`}
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">Topic {index + 1}</span>
                <Button variant="ghost" size="sm" onClick={() => removeDeniedTopic(index)}>
                  Remove
                </Button>
              </div>
              <Field label={<span className="sr-only">Denied topic {index + 1} name</span>}>
                {props => (
                  <input
                    {...props}
                    type="text"
                    className="input"
                    placeholder="Name"
                    value={row.name}
                    onChange={event => updateDeniedTopic(index, { name: event.target.value })}
                  />
                )}
              </Field>
              <Field label={<span className="sr-only">Denied topic {index + 1} definition</span>}>
                {props => (
                  <textarea
                    {...props}
                    className="input"
                    rows={2}
                    placeholder="Definition"
                    value={row.definition}
                    onChange={event => updateDeniedTopic(index, { definition: event.target.value })}
                  />
                )}
              </Field>
              <Field label={<span className="sr-only">Denied topic {index + 1} examples</span>}>
                {props => (
                  <textarea
                    {...props}
                    className="input"
                    rows={2}
                    placeholder="Examples (comma or newline separated, up to 5)"
                    value={row.examples}
                    onChange={event => updateDeniedTopic(index, { examples: event.target.value })}
                  />
                )}
              </Field>
            </div>
          ))}
          <Button variant="secondary" onClick={addDeniedTopic}>
            Add denied topic
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Words</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          <TextArea
            label="Denied words (one per line)"
            rows={4}
            value={words}
            onChange={event => setWords(event.target.value)}
          />
          <Field label="Use the managed profanity word list">
            {props => (
              <input
                {...props}
                type="checkbox"
                className="h-4 w-4 rounded border-border text-primary-600"
                checked={managedProfanity}
                onChange={event => setManagedProfanity(event.target.checked)}
              />
            )}
          </Field>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>PII</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3" data-testid="pii-rows">
          {piiRows.map((row, index) => (
            <div
              key={index}
              className="flex flex-wrap items-end gap-3 p-3 rounded-lg border border-border"
              data-testid={`pii-row-${index}`}
            >
              <div className="flex-1 min-w-[10rem]">
                <Select
                  label="Entity type"
                  value={row.type}
                  onChange={event =>
                    updatePiiRow(index, { type: event.target.value as PiiEntityType })
                  }
                >
                  {PII_ENTITY_TYPES.map(type => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </Select>
              </div>
              <SegmentedField
                label="Action"
                aria-label={`PII rule ${index + 1} action`}
                options={PII_ACTION_OPTIONS}
                value={row.action}
                onChange={action => updatePiiRow(index, { action })}
              />
              <Button variant="ghost" size="sm" onClick={() => removePiiRow(index)}>
                Remove
              </Button>
            </div>
          ))}
          <Button variant="secondary" onClick={addPiiRow}>
            Add PII rule
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Contextual grounding</CardTitle>
        </CardHeader>
        <CardBody className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Input
            label="Grounding threshold (0-1)"
            type="number"
            step="0.05"
            min={0}
            max={1}
            value={groundingThreshold}
            onChange={event => setGroundingThreshold(event.target.value)}
          />
          <Input
            label="Relevance threshold (0-1)"
            type="number"
            step="0.05"
            min={0}
            max={1}
            value={relevanceThreshold}
            onChange={event => setRelevanceThreshold(event.target.value)}
          />
        </CardBody>
      </Card>

      {formError && (
        <Alert variant="error" data-testid="form-error">
          {formError}
        </Alert>
      )}

      {saveError && (
        <Alert variant="error" data-testid="save-error">
          Could not save: {saveError.message}
        </Alert>
      )}

      <div className="flex gap-2">
        <Button type="submit" loading={saving} loadingLabel="Saving…">
          Save
        </Button>
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
