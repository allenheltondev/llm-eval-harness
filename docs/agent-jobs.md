# Agent jobs: comparing models on real Updraft work

Nimbus usually runs the model itself. Agent jobs go the other way. Updraft
(software-factory-cli) runs coding agents on real GitHub issues, and each time a
job finishes, Updraft Cloud publishes an event. Nimbus stores those events and
compares the models that did the work.

```
Updraft Cloud                                     Nimbus (this stack)
-------------                                     -------------------
UpdraftTable stream
  -> JobFinishedEventsFunction
  -> account's default bus ── rule: updraft.cloud / Job Finished ──> AgentJobIngestFunction
                                                                       -> AGENTJOB# items (EvalTable)
                                                                     GET /api/v1/agent-jobs/compare
```

Both sides meet on the account's **default** EventBridge bus, so neither stack needs the
other's name and they can deploy in any order. Several Updraft stacks in one account share the
bus; each event's `stack` field tells them apart, and it is part of every stored job's key.

On Updraft's side (software-factory-cli):

- #110 publishes the event, and #111 adds each step's model and usage. Both have shipped.
- #112 adds pinned benchmark dispatch and is in progress.
- #116 would replay events for jobs that finished before a tenant opted in.

## Turning it on

There is nothing to configure in either stack. Updraft always publishes, and the harness always
subscribes whenever the eval worker is deployed (`make deploy-backend`). The ingest function
reuses the worker's zip. Two things remain:

1. **Opt tenants in, in Updraft.** Publishing is per tenant and off by default, because it is
   the control over whether a tenant's job data leaves Updraft. A platform admin opts a tenant
   in with `PUT /api/v1/admin/tenants/<installation-id>/job-finished-events` and the body
   `{"enabled":true}`. Events start from that point: earlier jobs are not sent (#116 tracks a
   replay).
2. **Smoke-test the harness side** (optional):

   ```sh
   make smoke-agent-jobs
   ```

   This sends one synthetic `Job Finished` event to the default bus, waits for the stack to
   store it, checks that the dead-letter queue gained nothing, and deletes the item again. The
   synthetic job uses a `smoke-<timestamp>` stack and is a `review` job, so even before it is
   deleted it can't appear in a default comparison. It needs AWS credentials that can call
   `PutEvents` and `GetItem`/`DeleteItem` on the table. It makes no model calls. It proves the
   harness half only; a real job finishing in Updraft for an opted-in tenant is what tests
   Updraft's side.

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
| `kind` | `implement` | The job kind compared: `implement`, `revision` or `review`. `all` mixes them (see below). Any other value, including a blank one, is rejected with `422`. |
| `repository`, `tenant_id`, `stack` | – | Keep only jobs that match. |
| `include_routed` | `false` | Keep tier-routed jobs (see below). |
| `include_benchmarks` | `false` | Keep jobs started by a benchmark. |

Each arm reports the following:

- **Its identity:** `model`, `effort`, `agent` and `adapter`. Each is set only when every job in
  the arm reports it and they all report the same value. Otherwise the field is `null`.
  - `mixed` lists the values that were reported. That's several values when, for example, an
    `adapter` arm holds two models, or one value when some jobs reported it and others didn't.
  - `unreported` counts the jobs that didn't report the field at all.
- **Success rate,** with a 95% Wilson interval. The denominator is only jobs with a known
  outcome (see below).
- **Rates where the jobs reported the data:** verification pass rate, PR-opened rate and merge
  rate.
- **Mean revision rounds** and **median duration**.
- **Cost (`cost`) and mean tokens:** taken from the credited step's own `usage`, so an arm is
  charged only for its own model's work. Costs are per job and per success, counted only over
  jobs that reported a cost; a job with no reported usage is never averaged in as zero.
- **Whole-job cost (`job_cost`):** every step summed, reviewer runs and revisions included. Use
  this for what a job cost to run, not for what one model cost.
- **`model_source`:** how each job's model was known. `response` means the provider's response
  named it. `configured` means only the tenant's configuration did, which a fallback may not
  have honoured. `unreported` means a model with no source, and `no_model` means none was named.
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
- **Only known outcomes count, judged on both fields.** Every job carries two outcomes, and the
  comparison reads both:
  - `outcome` comes from the microVM runner. It is `succeeded` whenever `updraft run-event`
    exits 0, which also happens when no agent did any work.
  - `updraft_outcome` is Updraft's own result for the run: whether the agent actually ran, and
    how that went.

  Every job left out is counted under `excluded`, by reason. The rules are applied in order:

  1. **Not the agent's doing (`not_attributable`):**
     - the runner says `launch_failed`, `throttled`, `superseded`, `microvm_gone`,
       `released_by_admin`, `terminated`, `runner_error` or `state_not_saved`; or
     - Updraft says no agent work was judged (`nothing-to-do`, `canceled`, `superseded`,
       `deferred`, `not-started`, `state-pending`, or `error`, meaning the command itself failed),
       or that the agent succeeded and only publishing to GitHub failed (`publish-failed`). This
       applies whatever the runner said.
  2. **A failure:** the runner says `failed` or `timed_out`, or the runner says `succeeded` while
     Updraft says `failed`.
  3. **A success:** both fields say `succeeded`.
  4. **Anything else (`unresolved_outcome`):** for example a missing outcome on either side (such
     as a runner success whose Updraft summary was lost), `unknown` or `unconfirmed`, or a value
     added later. These jobs are never counted as successes or failures, and a warning gives
     their number.
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
