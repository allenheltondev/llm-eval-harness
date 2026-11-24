# Agent jobs: comparing models on real Updraft work

Nimbus usually runs the model itself. Agent jobs go the other way. Updraft
(software-factory-cli) runs coding agents on real GitHub issues, and each time a
job finishes, Updraft Cloud publishes an event. Nimbus stores those events and
compares the models that did the work.

```
Updraft Cloud                                   Nimbus (this stack)
-------------                                   -------------------
UpdraftTable stream
  -> EventBridge Pipe (+ enrichment)
  -> bus `updraft-events` ── rule: updraft.cloud / Job Finished ──> AgentJobIngestFunction
                                                                     -> AGENTJOB# items (EvalTable)
                                                                   GET /api/v1/agent-jobs/compare
```

What Updraft has to add is tracked on its side:

- software-factory-cli #110 publishes the event.
- #111 adds the model and usage for each step.
- #112 adds pinned benchmark dispatch.

Until #110 ships, there is nothing to subscribe to. The pieces here deploy only when you give
them a bus name.

## Deploying

```sh
make deploy-backend UPDRAFT_EVENT_BUS_NAME=updraft-events
```

Updraft publishes the bus name in SSM as `/updraft/<stack>/event-bus-name`, and both stacks run
in the same account and region. This deploy needs the eval worker as well, because the ingest
function reuses the worker's zip. With `UpdraftEventBusName` left empty, which is the default,
none of the following resources exist.

| Resource | What it is |
|---|---|
| `AgentJobIngestFunction` | Validates each event and writes one `AGENTJOB#` item. Its policy grants `PutItem` only, and only on `AGENTJOB#*` keys. |
| `AgentJobIngestDeadLetterQueue` | Receives events that could not be stored. Two kinds land here: events EventBridge failed to deliver (after up to 24 h of retries), and events the function failed on after 2 retries. Messages are kept 14 days. |
| `AgentJobIngestDeadLetterAlarm` | Goes off when the queue holds any message. |

## The event (v1)

Updraft owns the contract. The full shape is in software-factory-cli #110, and
`server/nimbus/agentjobs/schemas.py` is how Nimbus reads it. The rules:

- **`source: updraft.cloud`, `detail-type: Job Finished`, `detail.schemaVersion: 1`.**
  - The rule matches on the source and detail-type only.
  - The function checks `schemaVersion` itself, so a v2 event fails into the DLQ instead of being
    quietly filtered out.
- **Only the identity is required:** `stack`, `tenantId`, `jobId`.
  - Every other field is optional. Events from before #111 still record their outcome, even
    without a model.
  - Fields Nimbus does not know about are ignored, because v1 only ever adds fields.
- **Storing is idempotent.** Each job is stored under `AGENTJOB#{stack}#{jobId}`, so a redelivered
  event simply overwrites its own item.
  - A retried job is a separate job with its own id.
  - Including `stack` in the key lets staging and production share one table.

The item shape is listed with the others in [cloud-evals.md](cloud-evals.md#dynamodb-item-shapes-existing-table-pksk--gsi1). Agent
jobs count as history and expire under `HistoryRetentionDays` like evaluations do.

## The API

All routes need the same sign-in as the rest of `/api/v1`. A server with no `NIMBUS_EVAL_TABLE`
answers `400 agent_jobs_unavailable`.

| Route | Returns |
|---|---|
| `GET /agent-jobs?limit=&cursor=` | Stored jobs, newest first. |
| `GET /agent-jobs/{stack}%23{jobId}` | One job. |
| `GET /agent-jobs/compare` | A comparison of jobs grouped by model (below). |

`compare` takes these query parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `since`, `until` | the last 30 days | The window, by finish time. At most 366 days. |
| `group_by` | `model` | What counts as one arm: `model`, `model_effort`, `agent` or `adapter`. |
| `kind` | `implement` | The job kind compared: `implement`, `revision` or `review`. `all` mixes them (see below). |
| `repository`, `tenant_id`, `stack` | – | Keep only jobs that match. |
| `include_routed` | `false` | Keep tier-routed jobs (see below). |
| `include_benchmarks` | `false` | Keep jobs started by a benchmark. |

Each arm reports the following:

- **Its identity:** `model`, `effort`, `agent` and `adapter`. Each is set only when every job in
  the arm agrees on it. When they differ (for example an `adapter` arm that holds two models),
  the field is `null` and `mixed` lists the values found.
- **Success rate,** with a 95% Wilson interval. The denominator is only jobs with a known
  outcome (see below).
- **Rates where the jobs reported the data:** verification pass rate, PR-opened rate and merge
  rate.
- **Mean revision rounds** and **median duration**.
- **Cost:** per job and per success, counted only over jobs that reported a cost.
- **Mean tokens.**
- **`versus_leader`:** the difference in success rate from the leader, with a Newcombe 95%
  interval and a `distinguishable` flag.

A job is credited to the step that did its work. For an implement or revision job, that is the
first step that wrote code. A review job is credited to its first review step. The reviewer
inside an implement job's loop judged the work and did not do it, so it never gets the credit.
If a job reports no step of the kind it needs, for example an implement job whose only reported
step is its reviewer, no step is credited and the job is grouped as `unknown`.

## What this can and cannot tell you

The comparison reports `basis: "observational"` because these are real jobs, and different
models were given different issues.

- **Tier-routed jobs are left out by default.** Tier routing sends harder or riskier issues to
  stronger models on purpose, so including those jobs would compare the work rather than the
  models. Jobs from round-robin pools and fixed agents are kept, because there the issue does not
  decide the model.
- **Only known outcomes count.** Updraft accepts any outcome string, so the comparison only counts
  outcomes it recognizes. Every job left out is counted under `excluded`, by reason.
  - A success is `succeeded`.
  - The agent's failures are `failed` and `timed_out`.
  - These outcomes say nothing about the agent and are left out as `not_attributable`:
    `launch_failed`, `throttled`, `superseded`, `microvm_gone`, `released_by_admin`, `terminated`,
    `runner_error` and `state_not_saved`.
  - Anything else is left out as `unresolved_outcome`. That includes a missing outcome, Updraft's
    `unknown` or `unconfirmed`, and any value added later. Such jobs are never counted as
    failures, and a warning gives their number.
- **One kind at a time.** Implement, revision and review jobs succeed by different standards, so
  the default compares `implement` jobs only. `kind=all` mixes every kind into the same arms. It
  is an explicit opt-in, and the response warns that each arm's rate blends the kinds.
- **Volume limits what you can conclude.** At around 100 jobs a month across two or three models,
  each arm gets roughly 30 to 40 comparable jobs, which gives intervals of about ±15 points.
  - A gap of 70% against 90% shows up within a month or two.
  - A gap of 75% against 85% needs on the order of 100 to 150 jobs per model.
  - The comparison says so: `distinguishable: false`, plus a warning whenever no arm can be told
    apart from the leader. An arm with fewer than 20 jobs is flagged `small_sample`.

To answer "which model should we use" quickly, run a paired benchmark: the same issues, once per
model. That needs software-factory-cli #112. Jobs it dispatches carry a `benchmark` field, and
this comparison leaves them out by default, so benchmark results never mix into the real-work
numbers.
