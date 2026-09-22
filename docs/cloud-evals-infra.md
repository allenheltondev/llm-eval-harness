# Cloud evaluation lane — infrastructure and worker host

How the cloud lane's worker is packaged, deployed and invoked.
`docs/cloud-evals.md` is the *contract* (item shapes, event ordering, reader
and writer rules); this document is the hosting decision behind it.

Claims below are tagged **[aws]**, **[repo]**, or **[measured]** (something
actually run on this branch).

## Decision: a Lambda, invoked asynchronously

The worker is an ordinary Python 3.12 Lambda (`EvalWorkerFunction`), arm64, with
the handler named directly as `evalharness.worker.lambda_app.handler`. The
FastAPI server starts an evaluation with `lambda:Invoke` and
`InvocationType="Event"`.

That async invoke is the entire design. AWS durably queues the event and
answers `202` in milliseconds, so:

- the server's `POST /evaluations` returns at once and never holds a connection
  open for a multi-minute job;
- the worker runs the **whole** evaluation inside one invocation, with no
  ack-then-keep-working trick to get right;
- the queueing, retry policy and concurrency are AWS's to manage, not ours.

Progress still flows entirely through DynamoDB, exactly as
`docs/cloud-evals.md` specifies — the transport for *starting* a job and the
transport for *observing* it were always separate concerns, which is what made
this host swap a small change.

### What replaced what

This lane previously ran on Amazon Bedrock AgentCore Runtime. The parts that
were specific to it are gone: the CodeZip artifact and its root-level entry
shim, the `bedrock-agentcore` SDK (and the `worker` extra that installed it),
the `@app.async_task` health-reporting trick that kept a session alive while a
background task ran, the padded `runtimeSessionId` derivation, and the
AgentCore execution role with its `bedrock-agentcore.amazonaws.com` trust
policy and X-Ray/CloudWatch-namespace grants.

What did *not* change: `evalharness/worker/ddb.py`, `interfaces.py`, the
DynamoDB item shapes, the reader, and the engine seam. The host was always the
thin part.

## The 15-minute ceiling

This is the one real constraint the move introduced, and it is worth being
explicit about rather than discovering it in production.

Lambda's maximum timeout is **900 seconds** **[aws]**, and the function is
configured at exactly that. An evaluation can legitimately want longer: up to
25 repeats at a concurrency of 3, plus grading, plus throttle backoff.

Being killed at the ceiling is the worst available outcome. The execution
environment disappears mid-write, and the evaluation sits at `running` until
its 90-day TTL with nothing to tell the reader it is never coming back.

So the worker stops itself first. `evalharness.worker.lambda_app.Deadline`
reads the invocation's own `get_remaining_time_in_millis()` and, with
`DEADLINE_MARGIN_SECONDS` (60s) to spare, reports itself through the engine's
`cancelled` seam — the one already polled between runs and before grading. The
engine stops at a clean boundary with every finished run persisted, and the
worker then settles the evaluation as `error` / `deadline_exceeded` rather than
the `cancelled` the engine would otherwise record, because nobody cancelled it.

The margin has to cover the terminal DynamoDB writes (an `eval_complete` event
plus the META update) with room for a retry, and it is only checked *between*
runs — so it also absorbs the tail of whatever run is in flight when the
deadline passes.

### Retries are off

`EventInvokeConfig.MaximumRetryAttempts: 0`. AWS retries a failed asynchronous
invocation twice by default **[aws]**; that is wrong here. The handler routes
every *evaluation* failure into DynamoDB rather than raising, so a retry would
only ever mean the invocation itself died — most likely by exhausting its 15
minutes — and re-running it would burn another 15 minutes and overwrite the
first attempt's state.

## The artifact

`scripts/package-eval-worker.sh` builds a plain Lambda zip: aarch64-manylinux
wheels resolved for `python3.12`, the `evalharness` package beside them, no
entry shim (the template names the handler by dotted path).

The S3 key is content-hashed. For an S3-sourced function the key **is** the
property CloudFormation watches, so a static key means CFN sees no change and
the old bundle keeps serving forever.

### Size is now a real constraint

Lambda's ceiling is **250 MB unzipped** across the function and its layers
**[aws]** — where the previous host allowed 750 MB. Unpruned this artifact is
**217 MB** **[measured]**, which is 87% of the limit and one dependency bump
from failing a deploy.

So the script prunes the same transitively-pulled packages the server artifact
does (`sympy`, `PIL`, `pillow.libs`, `mpmath` — none of which any evaluation
path imports), bringing it to **159 MB zipped from 46 MB** **[measured]**, and
then *hard fails* above 240 MB rather than letting the deploy discover it.
`EVAL_WORKER_PRUNE=0` shows the raw size.

## IAM

The function's role (SAM-generated from `Policies`) grants exactly what an
evaluation needs:

- `bedrock:InvokeModel` / `InvokeModelWithResponseStream` / `Converse` /
  `ConverseStream` on foundation models and inference profiles — the model
  under test, plus the judge;
- `bedrock:ApplyGuardrail`, for guardrail-backed runs;
- `dynamodb:GetItem` / `PutItem` / `UpdateItem` on `EvalTable` — no `Scan` and
  no `Query`, because the worker only ever touches items whose keys it already
  knows.

The server side is one statement: `lambda:InvokeFunction` on the worker's ARN,
added only when both halves are deployed (`ServerHasEvalWorker`).

No S3 grant is needed at runtime: Lambda reads the code bundle at deploy time
with its own service permissions, not the function's role.

## The worker's two seams

### To the evaluation engine

`evalharness/worker/interfaces.py` is the whole surface between the host and
`evalharness.evals`:

```python
async def run_evaluation(request: dict, emit, store, cancelled) -> EvalOutcome
```

It resolves `evalharness.evals.engine.execute_evaluation_with_seam` **lazily**,
at call time. Two things depend on that: the worker had to import cleanly while
the evals refactor was still in flight, and a Lambda cold start should not pay
to import the run engine (boto3 clients, strands, SQLModel) before an
invocation has arrived. `test_importing_the_worker_does_not_import_the_evals_engine`
pins it down in a subprocess.

The engine's seam takes a validated `EvaluationRequest`, so `parse_request`
bridges the wire JSON to the model — which keeps the worker from re-implementing
(and drifting from) the eval schema. `normalize_outcome` accepts either the full
`{status, result, error, run_ids}` envelope or a bare result dict, so a
misbehaving or older engine still reaches a terminal state rather than hanging
the evaluation at `running`.

### To DynamoDB

`evalharness/worker/ddb.py` implements the engine's `EvalStore` protocol
(`save_evaluation`, `load_run`, `save_run`) plus the emitter and cancel polling.
Two of `cloud-evals.md`'s writer rules are enforced *structurally* rather than
by convention:

**Terminal status is never visible before the result.** `finalize()` writes
`status`, `result`, `error` and `run_ids` in a single `UpdateItem`. There is no
item version in which a reader sees `status="completed"` and no `result`.

**`eval_complete` is always the last event.** The engine settles the row and
*then* publishes:

```python
seam.store.save_evaluation(status=status, result=result, error=error)
seam.publish(EvalCompleteEvent(status=status, result=result))
```

Written straight through, that order opens exactly the race the reader cannot
tolerate: the reader stops polling on terminal status, so an event appended
*after* the flip can be missed. So a terminal `save_evaluation` is **buffered**,
and flushed by `emit()` the moment `eval_complete` is durable — or by
`finalize()` with a synthesized `eval_complete` if the engine died before
publishing one. The engine does not have to know any of this.

### Two contract clarifications for the reader

`cloud-evals.md` leaves these slightly open; the writer resolves them as
follows, and the FastAPI reader must match.

1. **`META.run_ids` is a JSON *string*, not a native DynamoDB list** — decoded
   with the same `json.loads` as `config` and `result`. This matches the SQLite
   `evaluations.run_ids` TEXT column and the row's JSON-as-string convention
   throughout.
2. **Nullable fields are written as `NULL`, never omitted.** Every attribute the
   contract declares is always present on the item, so the reader never has to
   distinguish "absent" from "null".

The writer also adds one attribute the contract's table does not list:
`META.progress` (a JSON string, `{completed, failed, total}`), because the
engine writes it mid-flight and the SQLite lane has the column. Readers may
ignore it.

---

## What is proven, and what is not

**Proven [measured]:**

- The artifact builds, is a well-formed zip, and contains no `bedrock-agentcore`
  and no entry shim. Its pure-Python import chain resolves from the bundle.
- 159 MB unzipped, under the 250 MB ceiling, with a guard that fails the build
  above 240 MB.
- `sam validate --lint` accepts the template — and unlike
  `AWS::BedrockAgentCore::Runtime`, for which cfn-lint ships *no schema at all*,
  `AWS::Serverless::Function` is fully checked. Every property on this resource
  is now validated, which the previous host's never were.
- The handler, the deadline guard and the terminal-state writes are covered by
  tests against an in-memory DynamoDB, including that a deadline settles as
  `deadline_exceeded` rather than `cancelled` and keeps its finished runs.

**Not proven — needs a real deploy and a real evaluation:**

- **That the function boots on arm64.** The wheels are cross-compiled
  `aarch64-manylinux_2_28`; this cannot be verified from an x86_64 build
  machine, where the native extensions refuse to load by design.
  `EVAL_WORKER_PYTHON_PLATFORM` is the escape hatch if the runtime image turns
  out to be older than glibc 2.34.
- **That the async invoke reaches it.** The `InvocationType="Event"` call is
  stubbed against botocore's shipped service model, not AWS.
- **The deadline against a real clock.** `get_remaining_time_in_millis` is
  faked in tests; the margin's adequacy for the terminal writes is reasoned,
  not measured.
- **A full evaluation end to end** — the model calls, the judge, and the
  reader's NDJSON stream draining from DynamoDB while the worker writes to it.

## Operational note: agent versions are no longer minted

Worth recording, because it is what prompted the move. Every deploy that
changed server code changed the worker artifact's hash, which updated the
AgentCore runtime, which minted a **new agent version** — and the account hit
`maxAgentVersions` after a handful of deploys in one day **[measured]**,
failing the deploy with a 402 `ServiceLimitExceeded` and leaving the stack in
`UPDATE_ROLLBACK_FAILED`.

A Lambda has no equivalent per-deploy resource to accumulate. Updating the
function's code is an in-place update against no quota of this kind.
