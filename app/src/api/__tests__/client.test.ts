import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../client'
import { ApiError } from '../errors'
import type {
  EvalCompleteEvent,
  EvalRunCompletedEvent,
  EvalRunFailedEvent,
  EvalStartEvent,
  EvalStreamEvent,
  MetricsEvent,
  RunCompleteEvent,
  RunStartEvent,
  RunStreamEvent,
  ToolInputDeltaEvent,
  ToolResultEvent,
  ToolUseStartEvent
} from '../types'
import {
  bodyOf,
  fakeResponse,
  headersOf,
  jsonResponse,
  methodsOf,
  mockFetch,
  ndjsonResponse,
  splitInto,
  urlOf,
  urlsOf
} from './helpers'

const BASE = 'http://localhost:8000/api/v1'

// Read from disk rather than a Vite `?raw` import: the latter routes through
// Vite's asset-transform pipeline, which some non-browser test runners
// (Stryker's mutation test runner, in particular) don't apply consistently.
// `readFileSync` is plain Node and behaves the same everywhere. Built via
// `node:path`/`node:url` rather than `new URL(...)` directly — the jsdom test
// environment replaces the global `URL` with its own implementation, which
// Node's `fs` functions don't recognize as a `file:` URL.
const FIXTURES_DIR = dirname(fileURLToPath(import.meta.url))
const runStreamFixture = readFileSync(join(FIXTURES_DIR, 'fixtures/run-stream.ndjson'), 'utf8')

const noContent = () => fakeResponse({ status: 204, statusText: 'No Content' })

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('api.health / api.models', () => {
  it('GETs /health', async () => {
    const spy = mockFetch(jsonResponse({ status: 'ok' }))

    await api.health()

    expect(urlOf(spy)).toBe(`${BASE}/health`)
    expect(spy.mock.calls[0][1]?.method).toBe('GET')
  })

  it('surfaces cloud_evals.configured from /health', async () => {
    mockFetch(
      jsonResponse({
        status: 'ok',
        aws: { region: 'us-east-1', credentials: 'ok' },
        cloud_evals: { configured: true }
      })
    )

    const health = await api.health()

    expect(health.cloud_evals).toEqual({ configured: true })
  })

  it('GETs /models', async () => {
    const spy = mockFetch(jsonResponse({ models: [], cached: true }))

    const result = await api.models.list()

    expect(urlOf(spy)).toBe(`${BASE}/models`)
    expect(result.cached).toBe(true)
    // a server that hasn't shipped multi-provider yet omits `providers`
    expect(result.providers).toBeUndefined()
  })

  it('GETs /models with the multi-provider shape (models[].source + providers)', async () => {
    mockFetch(
      jsonResponse({
        models: [
          {
            model_id: 'llama3',
            name: 'Llama 3',
            provider: 'Ollama',
            supports_streaming: false,
            kind: 'foundation-model',
            source: 'ollama'
          }
        ],
        providers: {
          bedrock: { configured: true },
          anthropic: { configured: false },
          openai: { configured: false },
          ollama: { configured: true, reachable: false }
        },
        cached: false
      })
    )

    const result = await api.models.list()

    expect(result.models[0].source).toBe('ollama')
    expect(result.providers?.ollama).toEqual({ configured: true, reachable: false })
  })
})

describe('api.tools', () => {
  it('GETs /tools and returns the toolsets with their tool names', async () => {
    const spy = mockFetch(
      jsonResponse({
        toolsets: [{ name: 'fraud-detection', tools: ['lookupAccount', 'flagTransaction'] }]
      })
    )

    const result = await api.tools()

    expect(urlOf(spy)).toBe(`${BASE}/tools`)
    expect(spy.mock.calls[0][1]?.method).toBe('GET')
    expect(result.toolsets).toEqual([
      { name: 'fraud-detection', tools: ['lookupAccount', 'flagTransaction'] }
    ])
  })

  it('passes the AbortSignal through', async () => {
    const spy = mockFetch(jsonResponse({ toolsets: [] }))
    const controller = new AbortController()

    await api.tools({ signal: controller.signal })

    expect(spy.mock.calls[0][1]?.signal).toBe(controller.signal)
  })
})

describe('api.runs', () => {
  it('forces stream:false on create', async () => {
    const spy = mockFetch(jsonResponse({ id: 'r1', status: 'completed' }))

    await api.runs.create({ model_id: 'm', user_prompt: 'hi', stream: true })

    expect(urlOf(spy)).toBe(`${BASE}/runs`)
    expect(bodyOf(spy)).toEqual({ model_id: 'm', user_prompt: 'hi', stream: false })
  })

  it('lists with snake_case filters and cursor pagination', async () => {
    const spy = mockFetch(jsonResponse({ items: [], next_cursor: null }))

    await api.runs.list({
      model_id: 'anthropic.claude-3-sonnet',
      status: 'completed',
      cursor: 'c1',
      limit: 50
    })

    expect(urlOf(spy)).toBe(
      `${BASE}/runs?model_id=anthropic.claude-3-sonnet&status=completed&cursor=c1&limit=50`
    )
  })

  it('lists cloud-lane runs with ?execution=cloud', async () => {
    const spy = mockFetch(jsonResponse({ items: [], next_cursor: null }))

    await api.runs.list({ execution: 'cloud' })

    expect(urlOf(spy)).toBe(`${BASE}/runs?execution=cloud`)
  })

  it('gets and deletes a run', async () => {
    const spy = mockFetch(jsonResponse({ id: 'r1' }), noContent())

    await api.runs.get('r1')
    await api.runs.remove('r1')

    expect(urlOf(spy, 0)).toBe(`${BASE}/runs/r1`)
    expect(spy.mock.calls[1][1]?.method).toBe('DELETE')
  })

  it('exports all runs as an NDJSON GET stream', async () => {
    const spy = mockFetch(
      ndjsonResponse(['{"id":"r1","status":"completed"}\n{"id":"r2","status":"error"}\n'])
    )
    const ids: string[] = []

    await api.runs.exportAll({ status: 'completed' }, { onEvent: run => ids.push(run.id) })

    expect(urlOf(spy)).toBe(`${BASE}/runs?status=completed`)
    expect(headersOf(spy).accept).toBe('application/x-ndjson')
    expect(ids).toEqual(['r1', 'r2'])
  })
})

describe('api.runs.stream', () => {
  it('delivers the full event sequence in order from a realistic stream', async () => {
    // Feed the fixture in awkward slices so chunk boundaries land mid-line.
    mockFetch(ndjsonResponse(splitInto(runStreamFixture, 7)))
    const events: RunStreamEvent[] = []

    await api.runs.stream(
      { model_id: 'anthropic.claude-3-sonnet', user_prompt: 'where is B456?' },
      { onEvent: event => events.push(event) }
    )

    expect(events.map(event => event.type)).toEqual([
      'run_start',
      'text_delta',
      'text_delta',
      'text_delta',
      'tool_use_start',
      'tool_input_delta',
      'tool_result',
      'metrics',
      'run_complete'
    ])

    const start = events[0] as RunStartEvent
    expect(start.run_id).toBe('01JBQZ7K3M9X2VQY8N4F6TWR5A')
    expect(start.model_id).toBe('anthropic.claude-3-sonnet')
    expect(typeof start.ts).toBe('string')

    expect(
      events
        .filter((event): event is Extract<RunStreamEvent, { type: 'text_delta' }> =>
          event.type === 'text_delta'
        )
        .map(event => event.text)
        .join('')
    ).toBe('Checking order B456')

    const toolStart = events[4] as ToolUseStartEvent
    expect(toolStart).toEqual({
      type: 'tool_use_start',
      tool_use_id: 'tooluse_9dK1aQ',
      name: 'getCarrierStatus'
    })

    // The wire field is "json" (the server attribute is json_text).
    const inputDelta = events[5] as ToolInputDeltaEvent
    expect(inputDelta.tool_use_id).toBe('tooluse_9dK1aQ')
    expect(JSON.parse(inputDelta.json)).toEqual({ order_id: 'B456' })

    const toolResult = events[6] as ToolResultEvent
    expect(toolResult.input).toEqual({ order_id: 'B456' })
    expect(toolResult.error).toBeNull()
    expect(typeof toolResult.duration_ms).toBe('number')

    const metrics = events[7] as MetricsEvent
    expect(metrics.total_tokens).toBe(498)
    expect(metrics.cycle_count).toBe(2)

    const complete = events[8] as RunCompleteEvent
    expect(complete.status).toBe('completed')
    expect(complete.run_id).toBe(start.run_id)
    expect(complete.final_text).toContain('B456')
  })

  it('POSTs stream:true with the run request body', async () => {
    const spy = mockFetch(ndjsonResponse([runStreamFixture]))

    await api.runs.stream(
      { model_id: 'm', user_prompt: 'p', toolset: 'fraud-detection', inference: { temperature: 0.2 } },
      { onEvent: () => undefined }
    )

    expect(urlOf(spy)).toBe(`${BASE}/runs`)
    expect(bodyOf(spy)).toEqual({
      model_id: 'm',
      user_prompt: 'p',
      toolset: 'fraud-detection',
      inference: { temperature: 0.2 },
      stream: true
    })
  })
})

describe('api.evaluations', () => {
  it('creates, lists, gets, streams events and cancels', async () => {
    const detail = {
      id: 'e1',
      ts: '2026-08-11T00:00:00Z',
      kind: 'determinism',
      status: 'running',
      config: {},
      run_ids: [],
      result: null,
      progress: null,
      error: null
    }
    const spy = mockFetch(
      jsonResponse(detail, 202),
      jsonResponse({ items: [detail], next_cursor: null }),
      jsonResponse(detail),
      ndjsonResponse([
        '{"type":"eval_start","evaluation_id":"e1","kind":"determinism","n":2}\n',
        '{"type":"run_started","index":0}\n',
        '{"type":"run_completed","index":0,"run_id":"r1","status":"completed","summary":{"output_chars":12,"tool_calls":1,"duration_ms":900}}\n',
        '{"type":"run_failed","index":1,"error":{"code":"throttled"}}\n',
        '{"type":"grading_started"}\n',
        '{"type":"eval_complete","status":"completed","result":null}\n'
      ]),
      noContent()
    )

    await api.evaluations.create({
      kind: 'determinism',
      run_config: { model_id: 'm', user_prompt: 'p' },
      n: 2,
      grader: { model_id: 'grader-model', system_prompt: 'grade it' }
    })
    await api.evaluations.list({ kind: 'determinism', limit: 10 })
    await api.evaluations.get('e1')

    const events: EvalStreamEvent[] = []
    await api.evaluations.events('e1', { onEvent: event => events.push(event) })
    await api.evaluations.cancel('e1')

    expect(urlsOf(spy)).toEqual([
      `${BASE}/evaluations`,
      `${BASE}/evaluations?kind=determinism&limit=10`,
      `${BASE}/evaluations/e1`,
      `${BASE}/evaluations/e1/events`,
      `${BASE}/evaluations/e1`
    ])
    expect(methodsOf(spy)).toEqual([
      'POST',
      'GET',
      'GET',
      'GET',
      'DELETE'
    ])
    expect(bodyOf(spy, 0)).toEqual({
      kind: 'determinism',
      run_config: { model_id: 'm', user_prompt: 'p' },
      n: 2,
      grader: { model_id: 'grader-model', system_prompt: 'grade it' }
    })

    expect(events.map(event => event.type)).toEqual([
      'eval_start',
      'run_started',
      'run_completed',
      'run_failed',
      'grading_started',
      'eval_complete'
    ])
    const start = events[0] as EvalStartEvent
    expect(start.n).toBe(2)
    const completed = events[2] as EvalRunCompletedEvent
    expect(completed.summary).toEqual({ output_chars: 12, tool_calls: 1, duration_ms: 900 })
    const failed = events[3] as EvalRunFailedEvent
    expect(failed.error).toEqual({ code: 'throttled' })
    const done = events[5] as EvalCompleteEvent
    expect(done.status).toBe('completed')
  })

  it('lists cloud-lane evaluations with ?execution=cloud', async () => {
    const spy = mockFetch(jsonResponse({ items: [], next_cursor: null }))

    await api.evaluations.list({ execution: 'cloud' })

    expect(urlOf(spy)).toBe(`${BASE}/evaluations?execution=cloud`)
  })

  it('forwards execution:"cloud" on create', async () => {
    const spy = mockFetch(jsonResponse({ id: 'e1', status: 'pending' }, 202))

    await api.evaluations.create({
      kind: 'determinism',
      run_config: { model_id: 'm', user_prompt: 'p' },
      execution: 'cloud'
    })

    expect(bodyOf(spy)).toMatchObject({ execution: 'cloud' })
  })

  it('surfaces a 409 conflict when cancelling a finished evaluation', async () => {
    mockFetch(
      fakeResponse({
        status: 409,
        statusText: 'Conflict',
        text: JSON.stringify({
          error: {
            code: 'conflict',
            message: "Evaluation 'e1' already finished",
            detail: { status: 'completed' }
          }
        })
      })
    )

    const error = (await api.evaluations.cancel('e1').catch(e => e)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('conflict')
    expect(error.status).toBe(409)
    expect(error.detail).toEqual({ status: 'completed' })
  })
})

describe('api.guardrails', () => {
  const detail = {
    id: 'g1',
    arn: 'arn:aws:bedrock:us-east-1:1:guardrail/g1',
    name: 'strict',
    description: null,
    version: 'DRAFT',
    status: 'READY',
    createdAt: '2026-08-01T00:00:00Z',
    updatedAt: null,
    contentPolicy: { filters: [{ type: 'HATE', inputStrength: 'HIGH', outputStrength: 'LOW' }] },
    deniedTopics: [],
    wordPolicy: null,
    piiPolicy: null,
    contextualGrounding: null,
    blockedInputMessage: 'no',
    blockedOutputMessage: 'no'
  }

  it('lists, gets, creates, updates and removes with camelCase bodies', async () => {
    const spy = mockFetch(
      jsonResponse({ guardrails: [detail] }),
      jsonResponse(detail),
      jsonResponse(detail, 201),
      jsonResponse(detail),
      noContent()
    )

    await api.guardrails.list({ max_results: 10 })
    await api.guardrails.get('g1', { version: '2' })
    await api.guardrails.create({
      name: 'strict',
      contentPolicy: { filters: [{ type: 'HATE', inputStrength: 'HIGH' }] },
      blockedInputMessage: 'no'
    })
    await api.guardrails.update('g1', { name: 'strict', deniedTopics: [] })
    await api.guardrails.remove('g1', { version: '2' })

    expect(urlsOf(spy)).toEqual([
      `${BASE}/guardrails?max_results=10`,
      `${BASE}/guardrails/g1?version=2`,
      `${BASE}/guardrails`,
      `${BASE}/guardrails/g1`,
      `${BASE}/guardrails/g1?version=2`
    ])
    expect(methodsOf(spy)).toEqual([
      'GET',
      'GET',
      'POST',
      'PUT',
      'DELETE'
    ])
    expect(bodyOf(spy, 2)).toEqual({
      name: 'strict',
      contentPolicy: { filters: [{ type: 'HATE', inputStrength: 'HIGH' }] },
      blockedInputMessage: 'no'
    })
  })

  it('lists and publishes versions', async () => {
    const spy = mockFetch(
      jsonResponse({ versions: [{ id: 'g1', version: 'DRAFT', description: null }] }),
      jsonResponse({ id: 'g1', version: '1', description: 'first cut' }, 201)
    )

    await api.guardrails.versions.list('g1')
    const published = await api.guardrails.versions.create('g1', { description: 'first cut' })

    expect(urlOf(spy, 0)).toBe(`${BASE}/guardrails/g1/versions`)
    expect(urlOf(spy, 1)).toBe(`${BASE}/guardrails/g1/versions`)
    expect(spy.mock.calls[1][1]?.method).toBe('POST')
    expect(bodyOf(spy, 1)).toEqual({ description: 'first cut' })
    expect(published.version).toBe('1')
  })
})
