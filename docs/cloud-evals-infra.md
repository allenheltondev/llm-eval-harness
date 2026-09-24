# Cloud evaluation lane — infrastructure and worker host

How the cloud lane's worker is packaged, deployed and invoked.
`docs/cloud-evals.md` is the *contract* (item shapes, event ordering, reader
and writer rules); this document is the hosting decision behind it.

Claims below are tagged **[aws]**, **[repo]**, or **[measured]** (something
actually run on this branch).

## Decision: a Lambda, invoked asynchronously

The worker is an ordinary Python 3.12 Lambda (`EvalWorkerFunction`), arm64, with
the handler named directly as `nimbus.worker.lambda_app.handler`. The
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

What did *not* change: `nimbus/worker/ddb.py`, `interfaces.py`, the
DynamoDB item shapes, the reader, and the engine seam. The host was always the
thin part.

## The 15-minute ceiling

This is the one real constraint the move introduced, and it is worth being
explicit about rather than discovering it in production.

Lambda's maximum timeout is **900 seconds** **[aws]**, and the function is
configured at exactly that. An evaluation can legitimately want longer: up to
25 repeats at a concurrency of 3, plus grading, plus throttle backoff.

Being killed at the ceiling is the worst available outcome. The execution
environment disappears mid-write, and the evaluation sits at `running` --
forever, with history kept by default -- with nothing to tell the reader it is
never coming back.

So the worker stops itself first. `nimbus.worker.lambda_app.Deadline`
reads the invocation's own `get_remaining_time_in_millis()` and, with
`DEADLINE_MARGIN_SECONDS` (60s) to spare, reports itself through the engine's
`cancelled` seam — the one already polled between runs and before grading. The
engine stops at a clean boundary with every finished run persisted, and the
worker then settles the evaluation as `error` / `deadline_exceeded` rather than
the `cancelled` the engine would otherwise record, because nobody cancelled it.

The margin has to cover the terminal DynamoDB writes (an `eval_complete` event
plus the META update) with room for a retry.

### The cooperative stop is a request, not a guarantee

`cancelled` is polled *between* runs and before grading, and nowhere else. So
the 60-second margin cannot bound what it has to absorb: a single streaming
model call, or the whole grading phase, can begin with 61 seconds left and run
for minutes with nothing polling anything. Lambda then kills the environment
mid-write and the evaluation reads `running` until its TTL — the exact outcome
the deadline exists to prevent.

So there is a second, enforcing bound. The engine runs as its own task, waited
on for `remaining − HARD_DEADLINE_RESERVE_SECONDS` (25s); if it has not
finished by then it is cancelled rather than asked, and settles as `error` /
`deadline_exceeded` with the runs that finished before the stall.

The two reserves are deliberately different sizes. The cooperative stop trips
first (60s > 25s) and gets its chance, because stopping at a run boundary keeps
the run in progress; the hard stop only fires when that was ignored, and loses
the run in flight. Losing one run beats losing the evaluation's terminal state.

### Why the reason is recorded before the engine stops

The engine only knows one way to stop: it settles `cancelled` and publishes
`eval_complete`. Both happen inside the engine — on the cooperative path and
on the asyncio-cancel path alike — and `eval_complete` is what makes
`DynamoEvalStore` flush its buffered terminal state. So by the time the worker
regains control after either deadline, the row already reads `cancelled`, is
terminal, and cannot be corrected: any further write raises. A deadline is not
a cancellation, and the user never asked for one.

So the worker tells the store *why* before the engine settles.
`DynamoEvalStore.stop_with(error)` arms a reason, and while it is armed a
terminal `cancelled` is written as `error` with that `error` — in the `META`
row and in the stream's closing `eval_complete` line, which must agree. The
cooperative stop arms it at the moment the deadline trips; the hard stop arms
it before cancelling the engine task. A user's cancel is checked first and
arms nothing, so it still settles as `cancelled`. Only `cancelled` is ever
reinterpreted: an evaluation that finished, or failed on its own, keeps its own
outcome.

**A user's cancel still wins at the hard stop.** A cancel that lands while a
call is stuck is never seen by the engine — it polls between runs — so the
hard stop is merely what *interrupts* it. At that moment the worker checks the
cancel flag before arming anything; if it is set, nothing is armed and the
evaluation settles `cancelled`, exactly as the cooperative path does by
checking the flag before the deadline.

This is also why the hard stop does not use `asyncio.wait_for`. `wait_for`
cancels the task and only then raises, leaving no moment in which to arm the
reason before the engine settles.

An earlier version corrected the row *afterwards*, and its tests passed
against hand-written engine doubles — doubles that called `save_evaluation`
and returned without ever publishing `eval_complete`, so nothing was flushed
and the correction appeared to work. Against the real engine both paths
reported `cancelled`. The tests that guard this now drive the real
`execute_evaluation_with_seam` over a real `DynamoEvalStore`, with the fake
model standing in for Bedrock and a stalling variant of it for the in-flight
case.

A clock that cannot be read imposes no hard bound at all: `Deadline.budget()`
returns `None` and the evaluation runs unbounded, because a broken clock must
not cut a healthy evaluation short.

### Delivery is at-least-once, so the claim is what keeps retries safe

Asynchronous invocation is **at-least-once** **[aws]**: the same event can
arrive twice whatever the retry setting, which only governs redelivery *after a
failure*. Two deliveries executing the same evaluation would buy every model run
twice and overwrite each other's `EVENT#` items, because each store numbers its
events from zero.

So the conditional `pending` → `running` update is an **ownership claim**, not a
status change: exactly one caller can move the row out of `pending`.
`DynamoEvalStore.mark_running()` returns whether it won, and a delivery that
lost executes nothing — it returns `{"status": "duplicate"}` and writes nothing
at all, because writing anything would step on the delivery that owns the job.

**An ambiguous claim is resolved, not guessed.** The claim's `UpdateItem` can
land in DynamoDB while its response is lost — and botocore may retry the call
itself, so the retry fails its condition against the write that already landed
and surfaces as a `ConditionalCheckFailed`. Read naively, both say "someone else
won", every redelivery then finds `running` and stands down, and the evaluation
reads `running` forever with nobody executing it. So the claim writes a
`claim_token` alongside `status`, and *any* failure is settled by a consistent
read of the row:

| Row after the failed call | Meaning | Worker |
| --- | --- | --- |
| `running`, our token | the write landed; only the reply was lost | owns it, executes |
| `pending` (or no status) | the write never landed; nothing claimed | re-raises, so Lambda redelivers |
| anything else | another delivery won, or it is terminal | `duplicate`, runs nothing |
| read-back fails too | unknowable | raises rather than guess |

The token is random per invocation and deliberately **not** the Lambda request
id: at-least-once duplicates are deliveries of the *same* event, and a token
they could share would let two of them both believe they had won.

**The claim is the point of no return, so everything fallible runs before
it.** Past the claim, any failure strands the row at `running`, because every
redelivery finds it claimed. Before it, a failure leaves `pending` and a retry
recovers it. That is why the scratch database is set up before
`mark_running()`: a full disk or a SQLite error there fails the invocation with
nothing claimed.

### Retries are on, because of the claim

`EventInvokeConfig.MaximumRetryAttempts: 2`. This used to be `0`, on the
reasoning that a retry could only mean the invocation had died mid-evaluation,
and re-running would burn another 15 minutes over the first attempt's state.
The claim removed that danger — a redelivery of a claimed evaluation runs
nothing — and a real case needs the retry:

**A failure before the claim.** If DynamoDB is throttled or unreachable as the
worker starts, `begin()` or `mark_running()` raises. The server has already
written the `pending` row by then. Returning an error body here would be
useless — nobody reads an async invocation's return value, and returning tells
Lambda the invocation *succeeded*, so there would be no retry and the row would
read `pending` forever. So the handler **raises**, Lambda redelivers, and the
retry claims the row and runs it. Nothing was bought on the failed attempt,
because nothing had been claimed.

What retries still cannot recover, listed so nobody has to rediscover them:

- **Stuck at `running`**, because the invocation got past the claim and then
  could not finish settling. That is an invocation killed after claiming, which
  the two deadline guards exist to prevent; a terminal write that fails because
  DynamoDB itself is unavailable at the end of the evaluation (logged, never
  retried, since by then the handler has returned normally); and the narrow case
  where the claim write landed *and* both it and the consistent read-back failed.
- **Stuck at `pending`**, because a failure before the claim outlasted every
  retry.

Both would need something outside the invocation to notice and settle them —
an on-failure destination, or a sweeper for rows that have not moved in longer
than an invocation can live. That is not built.

## The scratch database is per invocation

The run engine writes every run to SQLite, and `DynamoEvalStore.save_run`
mirrors it into a `RUN#` item; after that the local row is scratch. But Lambda
reuses both the process and `/tmp` across warm invocations, and `store.db`
keeps a module-level engine, so a single shared file would accumulate the
outputs and tool transcripts of every unrelated evaluation the environment ever
ran, until ephemeral storage became the failure mode.

`lambda_app.scratch_database()` gives each invocation a database of its own
under `<tmp>/nimbus-worker/`, via `store.db.scoped_db`, which disposes the
engine afterwards and restores whatever was active before. The directory is
wiped at the **start** of an invocation, which clears whatever a killed one left
behind, and again at the **end**, so a frozen idle environment holds no
evaluation's data. It is a directory rather than a file because SQLite runs in
WAL mode here and keeps `-wal` and `-shm` files beside the database, and
deleting only the `.db` would leave both behind.

## Who writes the pending row

The server, before it invokes — and the ordering is the contract.

`InvocationType="Event"` means the `202` from AWS says only that the event was
queued. The worker may not start, and so may not write `META`, for seconds;
longer on a cold start. The SPA follows its own `202` straight into
`GET /evaluations/{id}/events`, which preflights the row and `404`s while it is
absent. A successfully queued evaluation would surface to the user as an error.

Two details make this safe:

- **The row goes into DynamoDB, not through the server's history repository.**
  They are the same thing in a deployed stack, but not on a laptop driving the
  cloud lane against one, where history is SQLite. The detail and event routes
  read the repository *first* and fall back to DynamoDB only on
  `NotFoundError`, so a stray local row would shadow the worker's and replay an
  empty `pending` record forever.
- **Both sides may write it.** `DynamoEvalStore.begin` is a conditional put
  (`attribute_not_exists(pk)`) and leaves the existing item alone when it
  loses, so whichever of server and worker gets there first wins and the result
  is the same row either way.

If the invoke fails after the row is written, the server settles it as `error`
/ `eval_worker_unavailable` rather than leaving a `pending` that never moves.

## The artifact

`scripts/package-eval-worker.sh` builds a plain Lambda zip: aarch64-manylinux
wheels resolved for `python3.12`, the `nimbus` package beside them, no
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

`nimbus/worker/interfaces.py` is the whole surface between the host and
`nimbus.evals`:

```python
async def run_evaluation(request: dict, emit, store, cancelled) -> EvalOutcome
```

It resolves `nimbus.evals.engine.execute_evaluation_with_seam` **lazily**,
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

`nimbus/worker/ddb.py` implements the engine's `EvalStore` protocol
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
- The handler, both deadline guards and the terminal-state writes are covered
  by tests against an in-memory DynamoDB, **driving the real engine** rather
  than a double of it: that a cooperative deadline and a stalled in-flight run
  both settle as `deadline_exceeded` in the row *and* the stream's last line;
  that a real user cancel still reads `cancelled`; that a clock which cannot be
  read imposes no bound at all; that an outer cancellation neither leaks the
  engine task nor writes the row ahead of it; and that a second delivery loses
  the ownership claim and executes nothing.
- That the server writes the `pending` row *before* the invoke, that the row
  never lands in the local history repository, and that a failed invoke settles
  the row rather than leaving it `pending`.

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
