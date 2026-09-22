# Cloud evaluation lane — shared contract

Evaluations run in one of two lanes, chosen per launch in the UI:

- **local** (default): executed in-process by the FastAPI server, history in SQLite.
  Nothing leaves the machine except the Bedrock calls themselves.
- **cloud**: executed by a worker Lambda, with job state, progress events,
  and run records persisted to the stack's
  DynamoDB table (`EvalTable`). Durable across laptop/server restarts and
  reviewable from any machine pointed at the same stack.

This document is the contract between the three implementations (infra/worker,
FastAPI server, frontend). Field names here are normative.

## Request

`POST /api/v1/evaluations` gains one field:

```json
{ "execution": "local" | "cloud" }   // default "local"
```

Everything else in `EvaluationRequest` is unchanged. When `execution: "cloud"`
and the server is not configured for the cloud lane, respond 400
`{"error": {"code": "cloud_lane_unavailable", ...}}`.

`EvaluationDetail` gains `"execution": "local" | "cloud"`.

## DynamoDB item shapes (existing table: pk/sk + GSI1)

All items carry `expiresAt` (epoch seconds, now + 90 days) for TTL.

| Item | pk | sk | attributes |
|---|---|---|---|
| Eval meta | `EVAL#{evaluation_id}` | `META` | `id, ts (ISO), kind, status (pending\|running\|completed\|error\|cancelled), config (JSON string: stored_config incl. grader/rubric/n), run_ids (JSON list), result (JSON string \| null), error (JSON string \| null), seq_count (number), GSI1PK="EVAL", GSI1SK={ts}` |
| Progress event | `EVAL#{evaluation_id}` | `EVENT#{seq:08d}` | `seq (number), ts, event (JSON string — exactly the existing EvalStreamEvent wire shapes)` |
| Cancel flag | `EVAL#{evaluation_id}` | `CANCEL` | `ts` — presence means cancel requested; worker checks between runs and before grading |
| Run record | `RUN#{run_id}` | `META` | same fields as the SQLite `runs` table (id, ts, model_id, system_prompt, user_prompt, config, output, tool_transcript, metrics, guardrail_trace, status, error — JSON columns as JSON strings), plus `evaluation_id`, `GSI1PK="RUN", GSI1SK={ts}` |

Writer rules (worker):
- Events are appended with a strictly increasing `seq` starting at 0; `META.seq_count`
  is updated after each append (eventually consistent is fine — readers use it as
  a hint, not truth).
- `META.status` transitions: pending → running → (completed | error | cancelled).
  Terminal status is written only after `result`/`error` and all run items are written.
- Run items are written as each run finishes, before that run's `run_completed` event.

Reader rules (FastAPI):
- `GET /evaluations/{id}/events` for a cloud eval: Query `pk=EVAL#{id}, sk begins_with EVENT#`
  (ascending), stream each `event` as an NDJSON line, then poll (~1.5s) for `seq > last`
  until `META.status` is terminal; finish by synthesizing nothing — the worker's own
  `eval_complete` event is the last line, exactly as in the local lane.
- `GET /evaluations/{id}` / list: read META. Cloud evals appear in `GET /evaluations`
  only when queried with `?execution=cloud` (GSI1 `EVAL` partition, by ts desc,
  cursor = base64url of GSI1SK+pk); default listing remains SQLite/local.
- `GET /runs/{id}`: SQLite first, then DDB `RUN#{id}/META` fallback.
  `GET /runs?execution=cloud`: GSI1 `RUN` partition listing.
- `DELETE /evaluations/{id}` on a running cloud eval: put the CANCEL item, return 204
  immediately (cancellation is best-effort/async; UI already tolerates this).

## Invocation

FastAPI → the worker Lambda via `lambda:Invoke` with
`InvocationType="Event"` and a JSON payload
`{ "evaluation_id": ..., "request": <EvaluationRequest minus execution> }`.

The invoke is asynchronous, so AWS queues the event and answers immediately
while the worker runs the whole evaluation inside one invocation: FastAPI never
holds a connection open for it.

**The payload is capped at 1,000,000 bytes.** An asynchronous `lambda:Invoke`
accepts at most 1 MB [aws: raised from 256 KB in October 2025], and
`EvaluationRequest` bounds none of the prompts, the rubric or `run_ids`, while
the Function URL accepts bodies up to 6 MB. So the server serializes the worker
payload and measures it *before* writing anything, and a request over the limit
is a `413 evaluation_too_large` with `detail: {bytes, limit_bytes}` — no
`pending` row is created and nothing is invoked. The limit sits at 10^6 rather
than 2^20 so it can never admit a payload AWS counts as over. A local-lane
evaluation has no such limit, and the error says so. Any other refusal from
`lambda:Invoke` (throttling, access denied, an unreachable endpoint) is a
`502 eval_worker_unavailable` naming the AWS error, never a bare 500.

**The server writes META `pending` before it invokes.** The `202` from AWS only
means the event was queued — the worker may not start for seconds, longer on a
cold start — so the id in the `202` would otherwise 404 against
`/evaluations/{id}` and its event stream. Both sides may write the item; the
worker's `begin` is a conditional put, so whichever goes first wins.

The worker's conditional `pending` → `running` update is an **ownership
claim**. Asynchronous delivery is at-least-once, so a duplicate event can
arrive; the delivery that loses the claim executes nothing.

An evaluation that would exceed Lambda's 15-minute limit is stopped
cooperatively at a run boundary, or cancelled outright if it is inside a run or
grading when the hard bound is reached, and settled as `error` with code
`deadline_exceeded`, keeping whatever runs finished. See
`docs/cloud-evals-infra.md`.

The worker reuses `evalharness`'s existing engine/evals/tools code; the ONLY
behavioral difference is the emitter (DDB writes instead of asyncio queue) and
the store (DDB items instead of SQLite). The eval engine gets an emitter/store
seam to make that swap injectable.

## Configuration

Server (pydantic-settings, `EVALHARNESS_` prefix):
- `eval_function_name: str | None` — worker Lambda name; None = cloud lane unavailable.
- `eval_table: str | None` — DynamoDB table name (the stack's `TableName` output; also
  the deployed server's history store).

Worker (env): `TABLE_NAME`, `AWS_REGION`. Model/judge config arrives in the payload.

Health: `GET /health` gains `"cloud_evals": {"configured": bool}`.

## Frontend

- Eval launcher: a "Run location" toggle — **This machine** (default) vs
  **Cloud — persisted** — with copy noting cloud sends prompts and outputs to
  your AWS account's DynamoDB table. Disabled with a tooltip when
  health says the lane is unconfigured. Choice remembered in settings
  (`defaultEvalExecution`).
- Eval list: lane badge per row (`local` | `cloud`); a filter to view cloud
  evaluations (drives `?execution=cloud`).
- Progress/result views are lane-agnostic — same events, same result shape.
