/**
 * The typed API client: one thin function per backend endpoint.
 *
 * Nothing here holds state, caches, or retries — it maps arguments onto URLs
 * and response bodies onto the types in `./types`. Paths are relative to
 * `/api/v1` (see `http.apiUrl`).
 */

import { http } from './http'
import { getNdjson, streamNdjson, type StreamOptions } from './stream'
import type {
  EvalStreamEvent,
  EvaluationDetail,
  EvaluationListParams,
  EvaluationRequest,
  GuardrailConfig,
  GuardrailDetail,
  GuardrailListResponse,
  GuardrailVersionCreateRequest,
  GuardrailVersionListResponse,
  GuardrailVersionSummary,
  HealthResponse,
  ModelListResponse,
  Page,
  RunDetail,
  RunListFilters,
  RunListParams,
  RunRequest,
  RunStreamEvent,
  RunSummary,
  ToolsResponse
} from './types'

/** Options accepted by every plain (non-streaming) call. */
export interface CallOptions {
  signal?: AbortSignal
}

const encode = encodeURIComponent

/* -------------------------------------------------------------------------- */
/* Models                                                                     */
/* -------------------------------------------------------------------------- */

const models = {
  /** `GET /models` -> `{models, cached}`. */
  list: (options: CallOptions = {}): Promise<ModelListResponse> =>
    http.get<ModelListResponse>('/models', { signal: options.signal })
}

/* -------------------------------------------------------------------------- */
/* Tools                                                                      */
/* -------------------------------------------------------------------------- */

/** `GET /tools` -> `{toolsets}`: the named toolsets a run can execute with. */
const tools = (options: CallOptions = {}): Promise<ToolsResponse> =>
  http.get<ToolsResponse>('/tools', { signal: options.signal })

/* -------------------------------------------------------------------------- */
/* Runs                                                                       */
/* -------------------------------------------------------------------------- */

const runs = {
  /**
   * `POST /runs` with `stream: false` — executes the run and resolves with the
   * persisted `RunDetail`. In-band failures surface as an `ApiError` (the
   * server converts them to a 500/502 envelope carrying `run_id`).
   */
  create: (body: RunRequest, options: CallOptions = {}): Promise<RunDetail> =>
    http.post<RunDetail>('/runs', { ...body, stream: false }, { signal: options.signal }),

  /**
   * `POST /runs` with `stream: true` — NDJSON. `onEvent` fires per event in
   * order; the promise resolves when `run_complete` has been delivered and the
   * server closes the stream. Aborting rejects with `StreamAbortedError` and
   * disconnects, which persists the run as `cancelled`.
   */
  stream: (body: RunRequest, options: StreamOptions<RunStreamEvent>): Promise<void> =>
    streamNdjson<RunStreamEvent>('/runs', { ...body, stream: true }, options),

  /**
   * `GET /runs` -> `Page<RunSummary>` (newest first). `execution: 'cloud'`
   * reads the DynamoDB `RUN` GSI1 partition instead of SQLite.
   */
  list: (params: RunListParams = {}, options: CallOptions = {}): Promise<Page<RunSummary>> =>
    http.get<Page<RunSummary>>('/runs', {
      query: {
        model_id: params.model_id,
        status: params.status,
        since: params.since,
        cursor: params.cursor,
        limit: params.limit,
        execution: params.execution
      },
      signal: options.signal
    }),

  /** `GET /runs/{id}`. */
  get: (runId: string, options: CallOptions = {}): Promise<RunDetail> =>
    http.get<RunDetail>(`/runs/${encode(runId)}`, { signal: options.signal }),

  /** `DELETE /runs/{id}` -> 204. */
  remove: (runId: string, options: CallOptions = {}): Promise<void> =>
    http.delete<void>(`/runs/${encode(runId)}`, { signal: options.signal }),

  /**
   * `GET /runs` with `Accept: application/x-ndjson` — streams every matching
   * run as a full `RunDetail` per line, ignoring cursor/limit.
   */
  exportAll: (
    filters: RunListFilters,
    options: StreamOptions<RunDetail>
  ): Promise<void> =>
    getNdjson<RunDetail>('/runs', {
      ...options,
      query: {
        model_id: filters.model_id,
        status: filters.status,
        since: filters.since
      }
    })
}

/* -------------------------------------------------------------------------- */
/* Evaluations                                                                */
/* -------------------------------------------------------------------------- */

const evaluations = {
  /**
   * `POST /evaluations` -> 202 with the `pending` row. The work then runs in
   * the background; follow it with `events(id)` and re-read it with `get(id)`.
   * A `determinism` body needs `run_config`, a `grade` body needs `run_ids` —
   * either missing is a 400 envelope, and an unknown `run_id` is a 404.
   */
  create: (body: EvaluationRequest, options: CallOptions = {}): Promise<EvaluationDetail> =>
    http.post<EvaluationDetail>('/evaluations', body, { signal: options.signal }),

  /**
   * `GET /evaluations` -> `Page<EvaluationDetail>`. `execution: 'cloud'` reads
   * the DynamoDB `EVAL` GSI1 partition instead of SQLite.
   */
  list: (
    params: EvaluationListParams = {},
    options: CallOptions = {}
  ): Promise<Page<EvaluationDetail>> =>
    http.get<Page<EvaluationDetail>>('/evaluations', {
      query: {
        kind: params.kind,
        status: params.status,
        cursor: params.cursor,
        limit: params.limit,
        execution: params.execution
      },
      signal: options.signal
    }),

  /** `GET /evaluations/{id}`. */
  get: (evaluationId: string, options: CallOptions = {}): Promise<EvaluationDetail> =>
    http.get<EvaluationDetail>(`/evaluations/${encode(evaluationId)}`, { signal: options.signal }),

  /**
   * `GET /evaluations/{id}/events` — NDJSON progress stream. Events are
   * retained, so a late subscriber replays the log from the beginning; the
   * stream always ends with `eval_complete`.
   */
  events: (evaluationId: string, options: StreamOptions<EvalStreamEvent>): Promise<void> =>
    getNdjson<EvalStreamEvent>(`/evaluations/${encode(evaluationId)}/events`, options),

  /**
   * `DELETE /evaluations/{id}` — cancel a running evaluation (204). Resolves
   * only once `cancelled` is persisted. An already-finished evaluation is a
   * 409 `conflict` (`ApiError.detail.status` carries its final status).
   */
  cancel: (evaluationId: string, options: CallOptions = {}): Promise<void> =>
    http.delete<void>(`/evaluations/${encode(evaluationId)}`, { signal: options.signal })
}

/* -------------------------------------------------------------------------- */
/* Guardrails                                                                 */
/* -------------------------------------------------------------------------- */

const versions = {
  /** `GET /guardrails/{id}/versions` -> `{versions}` (includes DRAFT). */
  list: (guardrailId: string, options: CallOptions = {}): Promise<GuardrailVersionListResponse> =>
    http.get<GuardrailVersionListResponse>(`/guardrails/${encode(guardrailId)}/versions`, {
      signal: options.signal
    }),

  /** `POST /guardrails/{id}/versions` -> 201; publishes the current DRAFT. */
  create: (
    guardrailId: string,
    body: GuardrailVersionCreateRequest = {},
    options: CallOptions = {}
  ): Promise<GuardrailVersionSummary> =>
    http.post<GuardrailVersionSummary>(`/guardrails/${encode(guardrailId)}/versions`, body, {
      signal: options.signal
    })
}

const guardrails = {
  /** `GET /guardrails` -> `{guardrails}`. */
  list: (
    params: { max_results?: number } = {},
    options: CallOptions = {}
  ): Promise<GuardrailListResponse> =>
    http.get<GuardrailListResponse>('/guardrails', {
      query: { max_results: params.max_results },
      signal: options.signal
    }),

  /** `GET /guardrails/{id}` (defaults to the DRAFT working copy). */
  get: (
    guardrailId: string,
    params: { version?: string } = {},
    options: CallOptions = {}
  ): Promise<GuardrailDetail> =>
    http.get<GuardrailDetail>(`/guardrails/${encode(guardrailId)}`, {
      query: { version: params.version },
      signal: options.signal
    }),

  /** `POST /guardrails` -> 201 `GuardrailDetail`. */
  create: (body: GuardrailConfig, options: CallOptions = {}): Promise<GuardrailDetail> =>
    http.post<GuardrailDetail>('/guardrails', body, { signal: options.signal }),

  /** `PUT /guardrails/{id}` — updates the DRAFT working copy. */
  update: (
    guardrailId: string,
    body: GuardrailConfig,
    options: CallOptions = {}
  ): Promise<GuardrailDetail> =>
    http.put<GuardrailDetail>(`/guardrails/${encode(guardrailId)}`, body, {
      signal: options.signal
    }),

  /** `DELETE /guardrails/{id}` -> 204 (a numbered `version` deletes just it). */
  remove: (
    guardrailId: string,
    params: { version?: string } = {},
    options: CallOptions = {}
  ): Promise<void> =>
    http.delete<void>(`/guardrails/${encode(guardrailId)}`, {
      query: { version: params.version },
      signal: options.signal
    }),

  versions
}

/* -------------------------------------------------------------------------- */

/** `GET /health` — never raises server-side. */
const health = (options: CallOptions = {}): Promise<HealthResponse> =>
  http.get<HealthResponse>('/health', { signal: options.signal })

export const api = {
  health,
  models,
  tools,
  runs,
  evaluations,
  guardrails
}

export type Api = typeof api
