# Nimbus

An evaluation harness for LLM prompts and agents, built on the
[Strands Agents SDK](https://strandsagents.com/). Run a prompt against a model with streaming
output and live tool-use, then grade the results with an LLM-as-judge determinism evaluation, and
keep the full run and evaluation history for comparison and export.

Evaluations are the product. A run is one execution of a prompt; an evaluation runs it `n` times
(or grades runs you already have) and scores the batch with `strands-agents-evals`. Everything
runs local-first; a serverless deployment (Lambda + CloudFront + Cognito) is optional.

The `nimbus` command line is the primary interface — see **[docs/cli.md](docs/cli.md)**. The
HTTP API and the web UI are the same engine behind a different door, and `nimbus serve`
starts them.

## Architecture

```
 +------------------+        +---------------------------+        +------------------------+
 |  app/ (React)    |  HTTP  |  server/ (FastAPI)         |        |  Bedrock / Anthropic / |
 |  Vite, :3000      | -----> |  uvicorn --reload, :8000   | -----> |  OpenAI / Ollama       |
 |  zero AWS creds   |        |  routes under /api/v1      |        |  (via Strands Agents)  |
 +------------------+        |                             |        +------------------------+
                              |  runs/evaluations/models/   |
                              |  tools/guardrails/health    |        +------------------------+
                              |                             | -----> |  Bedrock Guardrails    |
                              |  SQLite (server/data/)      |        |  (create/version/apply)|
                              |  run + evaluation history   |        +------------------------+
                              +--------------+--------------+
                                             | optional cloud lane
                                             v
                              +---------------------------+
                              |  infra/ (AWS SAM)           |
                              |  Lambda eval worker         |
                              |  DynamoDB: history + eval   |
                              |  state; Lambda server; S3 + |
                              |  CloudFront; Cognito pool   |
                              +---------------------------+
```

- **`server/`** — FastAPI + [Strands Agents SDK](https://strandsagents.com/). Executes runs
  against four providers, runs Python `@tool` handlers when a run names a toolset, runs
  determinism and grading evaluations (`strands-agents-evals`), authors Bedrock guardrails, and
  stores history in SQLite (`server/data/`). This is where the product lives; the UI is one
  client of it.
- **`app/`** — React + TypeScript SPA. Talks only to the server (`VITE_API_URL`); it never holds
  AWS credentials.
- **`infra/`** — one AWS SAM template: the DynamoDB table, the Lambda worker for the
  cloud evaluation lane, and (optionally) the server on Lambda, the SPA on S3 + CloudFront, and
  the Cognito user pool that gates it. No application code lives here.

## Prerequisites

- Node.js 22+ and npm
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Credentials for at least one model provider: the AWS credential chain for Bedrock, or an
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OLLAMA_HOST` (see [Configuration](#configuration))
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) —
  only to deploy `infra/`

## Quick start

```bash
make install
```

Then pick a mode:

### Fake mode — zero AWS, nothing to deploy

A scripted model and judge stand in for a real provider, so every feature (streaming, tool-use,
determinism evaluations, guardrail authoring UI) works end-to-end with no credentials at all.
This exists so CI and the E2E suite run free and hermetically; it is test infrastructure, not a
usage mode.

```bash
NIMBUS_FAKE_MODEL=1 make dev
```

### Real mode — live model calls

```bash
AWS_PROFILE=your-profile make dev          # Bedrock
OLLAMA_HOST=localhost:11434 make dev       # or a local model, no cloud account at all
```

The app opens at `http://localhost:3000` and talks to the server at `http://localhost:8000`.
History lives in `~/.local/share/nimbus/history.db`, shared with the `nimbus` command (a
checkout that already has `server/data/nimbus.db` keeps using it).

## Command line

Install `nimbus` once and use it from any directory — it needs Python 3.12 and
[uv](https://docs.astral.sh/uv/):

```bash
uv tool install "git+https://github.com/allenheltondev/llm-eval-harness#subdirectory=server"
nimbus doctor                                    # what is set up, and what to do about the rest
```

(In a checkout, `make install` puts it in `server/.venv` instead: `uv run nimbus` from `server/`.)
The full reference is **[docs/cli.md](docs/cli.md)**; the shape of it:

```bash
nimbus doctor                                    # check your setup
nimbus models                                    # what can I run against?
nimbus run -m <model-id> -p 'your prompt'        # one run, streamed
nimbus eval -m <model-id> -p '...' -n 10         # determinism experiment, graded
nimbus init && nimbus eval --suite suite.yaml    # a starter test suite, then run it
nimbus eval --run <id> --run <id>                # grade runs you already have
nimbus runs                                      # history, newest first
nimbus show <id>                                 # one run or evaluation, as JSON
nimbus serve                                     # the HTTP API the web UI talks to
nimbus login --url https://<your-stack>          # from now on, commands run on that stack
nimbus eval --suite suite.yaml                   #   so this shows in its web UI
nimbus --local runs                              #   and --local is this machine again
```

Two conventions worth knowing up front:

- **stdout is the product, stderr is the commentary.** `nimbus run ... > answer.txt` gets
  you the model's answer and nothing else, while token counts and tool calls still scroll past
  on the terminal. `--json` puts the NDJSON event stream on stdout instead — the same bytes the
  API serves, so scripts and wrappers read one format.
- **Exit codes mean something**: `0` finished, `1` the harness failed, `2` bad invocation, `130`
  cancelled. A grade of F is still `0` — the evaluation worked; it is telling you the answer is
  bad.

## Runs and toolsets

`POST /api/v1/runs` executes one prompt (`system_prompt`, `user_prompt`, `model_id`, `provider`,
inference settings, optional guardrail) and streams the result as NDJSON, or returns the finished
run with `stream: false`.

A run may name a **toolset** (`"toolset": "fraud-detection"`), a list of Python `@tool` handlers
the model can call during the run. `GET /api/v1/tools` lists what is registered. One example
ships; add your own next to it:

```python
# server/nimbus/tools/support.py
from strands import tool

@tool(name="escalate_ticket")
def escalate_ticket(ticket_id: str, priority: str, reason: str) -> dict:
    """Escalate a support ticket to management.

    Args:
        ticket_id: The ticket to escalate.
        priority: Urgency of the escalation.
        reason: Why this ticket needs escalation.
    """
    return {"success": True, "ticket_id": ticket_id, "priority": priority}
```

```python
# server/nimbus/tools/registry.py
_REGISTRY["support"] = [support.escalate_ticket]
```

## Evaluations

`POST /api/v1/evaluations` runs one of three kinds of evaluation, all graded by an
LLM-as-judge (`strands-agents-evals`):

- **`determinism`** — runs the same `run_config` `n` times (2–25, default 10) and grades the batch
  for response consistency.
- **`grade`** — grades a set of already-executed runs (`run_ids`) against a rubric.
- **`suite`** — runs a file of test cases, each an input plus an `expected` answer and/or
  `criteria`, and grades every answer against its own case: which cases pass, and why the others
  did not. `nimbus eval --suite cases.yaml`. See **[docs/suites.md](docs/suites.md)** and
  the commented example **[docs/examples/support-suite.yaml](docs/examples/support-suite.yaml)**.

The grader model, rubric, and system prompt are all configurable per request
(`grader.model_id`, defaults to `amazon.nova-pro-v1:0`; `grader.provider`, defaults to `bedrock`;
`grader.system_prompt`; `rubric`). The judge is independent of the graded runs — an OpenAI judge
grading Bedrock runs is a reasonable setup — but a non-Bedrock `grader.provider` **must** name its
own `grader.model_id`, since the default is a Bedrock model id and inheriting it elsewhere builds a
judge that can only fail at the provider. Progress
streams as NDJSON from `GET /api/v1/evaluations/{id}/events`; results and history live alongside
runs in the server's SQLite database.

### Execution lanes: local vs cloud

Evaluations run in one of two lanes, chosen per launch with the **Run location** toggle in the
Evals tab (`execution: "local" | "cloud"` on the API):

- **This machine** (default) — executed in-process by the server; history stays in local SQLite.
  Nothing leaves your machine except the model calls themselves.
- **Cloud — persisted** — executed by a worker Lambda, invoked asynchronously; job
  state, progress events, and every run record are persisted to the stack's DynamoDB table, so
  evaluations survive laptop/server restarts and are reviewable from any machine pointed at the
  same stack. Cloud evaluations appear under the **Cloud** filters in the Evals and History tabs.

The cloud lane is part of the stack: `make deploy-backend` packages the Python worker as an
worker Lambda zip alongside the server zip and deploys both. It prints the two values a
local server needs to use the deployed lane:

```bash
NIMBUS_EVAL_FUNCTION_NAME=llm-eval-harness-EvalWorker... # stack output EvalWorkerFunctionName
NIMBUS_EVAL_TABLE=llm-eval-harness-EvalTable-...              # stack output TableName
```

The UI disables the cloud option until `GET /health` reports `cloud_evals.configured`. Full design
and item shapes: `docs/cloud-evals.md`; infrastructure notes and first-deploy verification list:
`docs/cloud-evals-infra.md`.

### History: from the CLI, in the UI, kept

Every evaluation is recorded with where it was started — the web UI, the CLI, or another API
client — and the Evals tab labels it. After `nimbus login`, every `nimbus run` and `nimbus eval`
runs on the deployed stack from your terminal, so it lands in that stack's history and its UI
like any other ([docs/cli.md](docs/cli.md#running-on-a-deployed-stack)).

Opening an evaluation shows everything recorded about it: the grade and metrics, a suite's
per-case table (verdict, score for each repeat, the judge's reasoning, the case itself), the
configuration it ran with, and a link to every run. Each evaluation has its own address
(`/#/evals/<id>`, and `/#/runs/<id>` for a run), which is what the CLI prints and what **Copy
link** gives you.

Deployed history is **kept forever** by default. To expire it, deploy with
`HISTORY_RETENTION_DAYS=365 make deploy-backend` (the `HistoryRetentionDays` stack parameter);
evaluations and their runs then expire that many days after they were written. Progress events —
a replay log, not history — always expire after 90 days.

## Guardrails

`server/nimbus/routers/guardrails.py` authors AWS Bedrock Guardrails directly: create/update
operate on a mutable `DRAFT` working copy, `POST /guardrails/{id}/versions` publishes the current
draft as a new immutable numbered version, and any version (including `DRAFT`) can be applied to a
run. `GET /guardrails/{id}/versions` lists the full version history for a guardrail. Bedrock-only.

## Deploy to AWS (serverless)

Optional. When you want the harness on the internet, it is serverless end to end: **no containers
anywhere**, one Lambda for the FastAPI server (unchanged, behind the
[AWS Lambda Web Adapter](https://github.com/aws/aws-lambda-web-adapter)), S3 + CloudFront for the
SPA, DynamoDB for history, and a second Lambda for evaluations.

```bash
AWS_PROFILE=your-profile make deploy
```

One command, idempotent, prints the URL at the end. `deploy-backend` packages the server's arm64
Lambda zip and the eval worker's CodeZip, uploads both under content-hashed keys, and deploys the
SAM stack (table + worker runtime + server function + Function URL + S3 bucket + CloudFront
distribution + Cognito pool); `deploy-frontend` builds the SPA with `VITE_API_URL=/`, syncs it to
S3, and invalidates the CDN.

The result is a single origin: the SPA at `/`, the API at `/api/v1` on the same domain — so
there is no CORS in production and no API URL to configure in the app.

```
https://d1234abcd.cloudfront.net/            → the app
https://d1234abcd.cloudfront.net/api/v1/...  → the server
```

### Sign-in (Cognito)

The deployed app requires sign-in with a **Ready, Set, Cloud account**: the stack creates its own
app client and access group on the shared RSC Cognito pool (which `readysetcloud/rsc-core`
publishes in SSM, in the same account and region), the server verifies a bearer token from that
pool on every route except `/api/v1/health`, and the SPA
shows a sign-in screen until it has one. Nothing is baked into the build: the SPA learns from
`/health` whether sign-in is required and which pool to use, so the same `app/dist` works
locally (no pool, no gate) and deployed (pool, gate).

Anyone can create an RSC account (the sign-in screen offers it), but **an account is not
access**: an account can spend your model budget only once it is in the stack's group. Grant and
revoke from the CLI:

```bash
make grant-access EMAIL=you@example.com         # STACK_NAME=... for another stack
make revoke-access EMAIL=you@example.com
make create-user EMAIL=you@example.com          # no RSC account yet: invite by email, and grant
```

Someone signed in but not granted sees a no-access screen naming the group; after a grant they
sign out and back in, since the group travels in the token.

Sign-in talks to `cognito-idp.<region>.amazonaws.com` straight from the browser (no Hosted UI,
no redirect); the resulting ID token is sent as `Authorization: Bearer` and refreshed silently
for as long as the refresh token lasts (30 days). Sign out from the profile menu. Sign-in is
[`readysetcloud/rsc-core`](https://github.com/readysetcloud/rsc-core)'s
`@readysetcloud/ui/auth`, the same package every Ready, Set, Cloud app uses.

That group is `NIMBUS_AUTH_REQUIRED_GROUP` on the server: a valid token without it gets `403`.
It is named after the stack (`llm-eval-harness` for the default `STACK_NAME`), so every stack
sharing the pool gets its own; `make deploy-backend ACCESS_GROUP_NAME=...` names it yourself. The stack's own pool from before the shared one is kept, unused, as
`LegacyUserPoolId` -- retained so its accounts are not deleted with it.

> **What the gate is, and is not.** The Lambda Function URL stays `AuthType: NONE` and CloudFront
> stays open, because neither can be closed for a browser that POSTs (CloudFront OAC for
> function URLs needs a request-body hash browsers cannot send — see
> [`docs/serverless-deploy-infra.md`](docs/serverless-deploy-infra.md)). The gate is the
> application: every request through either door hits the same token check, and the `/health`
> endpoint is the one deliberate exception (it publishes nothing secret — region, pool id and
> public client id). Locally, with no pool configured, there is no gate at all; that is the
> intended local-first posture, not an oversight.

### Continuous deployment (GitHub Actions)

The pipeline follows `readysetcloud/newsletter-service`: a change-detection job decides what a
push touched, only the relevant validations run, and only the affected halves deploy.

| Workflow | Trigger | Deploys to |
| --- | --- | --- |
| `pull-request.yaml` | pull request to `main` (or manual) | the `Staging` GitHub environment |
| `deploy.yaml` | push to `main` (or manual) | the `Production` GitHub environment |

Each one runs `changes` → `pre-deploy-validation.yaml` → `deploy-backend` → `deploy-frontend`:

- **`changes`** classifies the changed files: `app/**` is the frontend; `server/**` and the two
  packaging scripts are the server; `infra/**`, the `Makefile` and the workflows are infra. The
  server and infra together are "backend". Nothing changed means nothing runs. Detection fails
  open, so an empty diff on a merge commit deploys rather than silently skipping.
- **`pre-deploy-validation.yaml`** is the reusable gate: app (lint, typecheck, coverage, build),
  server (ruff, pytest with coverage), template (`sam validate --lint`), and the Playwright E2E
  suite, each skipped when its inputs say the area did not change.
- **`deploy-backend`** runs `make deploy-backend`; **`deploy-frontend`** runs `make deploy-frontend`
  and depends on the backend job (it builds the SPA against the deployed stack). Both are the same
  targets a developer runs locally, so there is one deploy definition rather than a CI copy that
  drifts.
- Manual runs (`workflow_dispatch`) take a `force_deploy` input that bypasses change detection.
- A failed backend deploy dumps CloudFormation diagnostics (recent stack events, latest change
  set) into the log; a successful frontend deploy writes the app URL to the step summary and to
  the environment, so it shows up on the PR as a "Deployed to Staging" link.
- Dependabot PRs are validated but not deployed: they run with Dependabot's secret source, which
  cannot see the deploy role.
- `mutation.yaml` runs Stryker and mutmut as an advisory signal on every PR and push; it never
  blocks.

Both workflows deploy the stack `llm-eval-harness`; stage and prod are separate AWS accounts, so a
PR can never touch production. Deploys to one environment queue behind each other rather than
cancel, because interrupting `sam deploy` strands the stack in `UPDATE_IN_PROGRESS`. Fork PRs are
validated but not deployed, since they never receive environment secrets.

#### Setup owed by the human (same shape as `allenheltondev/content-tracking`)

Two GitHub **environments** on the repo, `Staging` and `Production`, each with one
environment-scoped secret, `AWS_DEPLOY_ROLE_ARN`: the IAM role in that environment's account that
GitHub Actions assumes through the OIDC provider. No access keys anywhere.

Each role's trust policy allows `token.actions.githubusercontent.com` with a subject condition
scoped to this repository. Staging accepts anything from the repo (every PR deploys there);
Production is scoped to `main`:

```json
{ "StringLike": { "token.actions.githubusercontent.com:sub": "repo:allenheltondev/llm-eval-harness:*" } }
```

```json
{ "StringLike": { "token.actions.githubusercontent.com:sub": "repo:allenheltondev/llm-eval-harness:ref:refs/heads/main" } }
```

Attach a deploy policy that grants CloudFormation, S3 (the SAM-managed bucket, the stack's artifact
bucket **and** the SPA bucket, including `s3:DeleteObject` for the `--delete` sync), Lambda, IAM
(role creation and `iam:PassRole`), DynamoDB, CloudFront (distributions, origin access controls,
functions, and `cloudfront:CreateInvalidation`, which cannot be resource-scoped), Cognito user
pools and, on the shared RSC pool (`arn:aws:cognito-idp:<region>:<account>:userpool/<shared pool id>`),
`cognito-idp:CreateUserPoolClient`, `UpdateUserPoolClient`, `DeleteUserPoolClient`,
`DescribeUserPoolClient`, `CreateGroup`, `UpdateGroup`, `DeleteGroup` and `GetGroup`, plus
`ssm:GetParameters` on `/readysetcloud/auth/user-pool-id` (the template resolves the pool from it).
`make grant-access` / `revoke-access` / `create-user` need `cognito-idp:AdminAddUserToGroup`,
`AdminRemoveUserFromGroup` and `AdminCreateUser` on that pool, for whoever runs them.

Three grants are easy to miss because nothing else in a typical SAM stack needs them, and **all
three have already broken a real deploy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadTheLambdaWebAdapterLayer",
      "Effect": "Allow",
      "Action": "lambda:GetLayerVersion",
      "Resource": "arn:aws:lambda:*:753240598075:layer:LambdaAdapterLayerArm64:*"
    },
    {
      "Sid": "ConfigureTheEvalWorkersAsyncRetries",
      "Effect": "Allow",
      "Action": [
        "lambda:PutFunctionEventInvokeConfig",
        "lambda:GetFunctionEventInvokeConfig",
        "lambda:UpdateFunctionEventInvokeConfig",
        "lambda:DeleteFunctionEventInvokeConfig"
      ],
      "Resource": "arn:aws:lambda:*:*:function:llm-eval-harness-EvalWorkerFunction-*"
    },
    {
      "Sid": "DeployDiagnostics",
      "Effect": "Allow",
      "Action": ["cloudformation:ListChangeSets", "cloudformation:DescribeChangeSet"],
      "Resource": "arn:aws:cloudformation:*:*:stack/llm-eval-harness/*"
    }
  ]
}
```

The first is the one that matters: the server Lambda attaches the AWS-published **Lambda Web
Adapter** layer, which lives in AWS's own account (`753240598075`), not yours. Without this grant
`ServerFunction` fails to create with `AccessDenied` on `lambda:GetLayerVersion` and the whole
stack rolls back — and because a Node-only SAM stack never attaches a cross-account layer, a
deploy role shared with other services will not already have it. (`LambdaAdapterLayerArn` is the
escape hatch if you would rather mirror the layer into your own account.)

The second covers the eval worker's `EventInvokeConfig` — the `MaximumRetryAttempts` setting
that lets Lambda redeliver an evaluation that failed before it was claimed
(`docs/cloud-evals-infra.md`). It is a separate CloudFormation resource with its own Lambda API
actions, which broad `lambda:*Function` grants do not cover. Without them the resource fails to
create, the whole update rolls back, and the rollback cannot clean the resource up either —
the stack ends `UPDATE_ROLLBACK_COMPLETE` with "one or more resources could not be deleted".
That is harmless (the stack is still updatable) but it is how the first production deploy of
the Lambda worker failed.

The third is only used by the workflows' "Diagnose failed deploy" step; without it that step
still runs but prints an `AccessDenied` instead of the change-set detail.

The resource patterns assume the default `STACK_NAME`; widen them if you deploy under another.

One sharp edge: `ServerArtifactKey` and `EvalWorkerArtifactKey` are CloudFormation parameters with
empty defaults, and an empty value deletes the corresponding resource. `make deploy-backend`
always passes both, so use it rather than a bare `sam deploy` once anything is deployed.

Details — the adapter layer, packaging and artifact size, the CloudFront origin/behaviour setup,
exact IAM, and the list of things only a real deploy can prove — are in
[`docs/serverless-deploy.md`](docs/serverless-deploy.md) (the contract) and
[`docs/serverless-deploy-infra.md`](docs/serverless-deploy-infra.md) (the build).

### Tearing the stack down

```
make destroy CONFIRM=llm-eval-harness
```

The `CONFIRM=` value has to match `STACK_NAME` exactly, because this deletes the history table,
the Cognito user pool and both buckets along with the stack — everything the deployment holds.

It exists because `aws cloudformation delete-stack` on its own does not work here. CloudFormation
refuses to delete a bucket that still has objects in it, and `ArtifactBucket` has versioning
enabled, so `aws s3 rm --recursive` does not empty it either: it writes delete markers, and every
version and marker has to be removed explicitly. A stack deleted the naive way lands in
`DELETE_FAILED` with the buckets orphaned — which then blocks the next deploy as well. `make
destroy` empties both buckets version by version first, then deletes the stack and waits for it.

Use a different `STACK_NAME=` to target a non-default stack, exactly as with `make deploy`.

## Make targets

| Target | What it does |
| --- | --- |
| `make install` | `app` (`npm ci`) + `server` (`uv sync --dev`) |
| `make dev` | Runs the server (`uvicorn --reload`, `:8000`) and app (`vite`, `:3000`) concurrently in one terminal; Ctrl-C stops both |
| `make dev-server` | Runs just the FastAPI server, for when you want its logs in their own terminal |
| `make dev-app` | Runs just the Vite dev server |
| `make lint` | `app` (`eslint`) + `server` (`ruff check`) |
| `make test` | `app` (`vitest`) + `server` (`pytest`) |
| `make validate-template` | `sam validate --lint` on `infra/template.yaml` (cfn-lint; no credentials needed) |
| `make e2e` | Playwright against the fake-model full stack |
| `make package-server` / `make package-eval-worker` | Build the server / eval worker zips. No AWS calls |
| `make deploy-backend` | Package both zips, upload them, `sam deploy` the whole stack |
| `make deploy-frontend` | Build the SPA against the deployed stack, sync it to S3, invalidate CloudFront |
| `make deploy` | [Full serverless deploy](#deploy-to-aws-serverless): `deploy-backend` then `deploy-frontend` |
| `make destroy CONFIRM=<stack>` | [Tear the stack down](#tearing-the-stack-down): empty both buckets (every object version), then `delete-stack` and wait |
| `make grant-access EMAIL=...` | Lets an existing Ready, Set, Cloud account use the deployed stack ([Sign-in](#sign-in-cognito)); `revoke-access` takes it away |
| `make create-user EMAIL=...` | Invites someone with no RSC account (Cognito emails a temporary password) and grants them the stack |

`make dev` runs both processes as background jobs of one recipe with a `trap ... EXIT INT TERM`
so `Ctrl-C` (or any exit) tears down both — no orphaned `uvicorn`/`vite` process left behind. If
that ever proves flaky in your shell, use the two `dev-server` / `dev-app` targets in separate
terminals instead; they run the exact same commands.

## Configuration

### Server (`server/`, env vars prefixed `NIMBUS_`)

Nimbus was called `evalharness` before, and the old `EVALHARNESS_*` names still
work: each is read when its `NIMBUS_*` name is not set. An existing
`server/data/evalharness.db` keeps being used as the default history until a
`nimbus.db` exists, and a CLI login saved under `~/.config/evalharness` is moved
to `~/.config/nimbus` the first time it is read.

| Env var | Default | Description |
| --- | --- | --- |
| `NIMBUS_AWS_REGION` | `us-east-1` | AWS region for Bedrock/Guardrails/DynamoDB calls |
| `NIMBUS_DB_PATH` | `~/.local/share/nimbus/history.db` | SQLite path for run/evaluation history (`$XDG_DATA_HOME/nimbus/` when set; `./data/nimbus.db` when that exists where the server starts) |
| `NIMBUS_CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed CORS origins (JSON list) |
| `NIMBUS_FAKE_MODEL` | `false` | Use the scripted fake model + judge instead of a real provider (test infrastructure) |
| `NIMBUS_ANTHROPIC_API_KEY` | *(unset)* | Anthropic API key — enables the `anthropic` provider (falls back to `ANTHROPIC_API_KEY`) |
| `NIMBUS_OPENAI_API_KEY` | *(unset)* | OpenAI API key — enables the `openai` provider (falls back to `OPENAI_API_KEY`) |
| `NIMBUS_OLLAMA_BASE_URL` | *(unset)* | Ollama server base URL, e.g. `http://localhost:11434` — enables the `ollama` provider (falls back to `OLLAMA_HOST`) |
| `NIMBUS_EVAL_FUNCTION_NAME` | *(unset)* | Name of the eval worker Lambda (stack output `EvalWorkerFunctionName`). With `EVAL_TABLE`, enables the cloud lane |
| `NIMBUS_EVAL_TABLE` | *(unset)* | DynamoDB table for cloud-eval state and deployed history (stack output `TableName`) |
| `NIMBUS_HISTORY_BACKEND` | `auto` | `sqlite` \| `dynamodb` \| `auto` (DynamoDB inside Lambda, SQLite elsewhere) |
| `NIMBUS_LOCAL_EVALS` | `auto` | `on` \| `off` \| `auto` (off inside Lambda) — whether evaluations may run in this process |
| `NIMBUS_AUTH_USER_POOL_ID` | *(unset)* | Cognito user pool to verify bearer tokens against. With `NIMBUS_AUTH_CLIENT_ID`, every route but `/health` requires a token; unset locally means no gate. The deployed stack injects both |
| `NIMBUS_AUTH_CLIENT_ID` | *(unset)* | The pool's app client id — what the SPA signs in with and what every accepted token's `aud`/`client_id` must equal |

AWS credentials themselves are **not** a setting — they come from the standard boto3 credential
chain (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, SSO, or an instance/task role).

Bedrock is the default provider and the only one that needs no extra configuration. Setting any of
the three keys above adds that provider's models to `GET /models` and lets runs, evaluations and
graders select it with `"provider": "anthropic" | "openai" | "ollama"`. Guardrails remain
Bedrock-only.

### App (`app/`)

| Env var | Default | Description |
| --- | --- | --- |
| `VITE_API_URL` | `http://localhost:8000` | Base URL of the FastAPI server |

Copy `app/.env.example` to `app/.env.local` to override it.

Set it to `/` for a same-origin build (what `make deploy` does behind CloudFront) — the API calls
then go to relative `/api/v1/...` paths. Note that `/`, not `""`, is the value: a blank
`VITE_API_URL` counts as unset and falls back to `http://localhost:8000`.

The web UI is built on the Ready, Set, Cloud design system,
[`@readysetcloud/ui`](https://www.npmjs.com/package/@readysetcloud/ui): its tokens and Tailwind
preset (every color, light and dark), its components (buttons, cards, inputs, status badges,
loading states), its `AppNav` rail with the RSC app launcher, and its sign-in flows. Colors are
never defined in the app, and a component the package ships is used rather than rebuilt; the
package's `AGENTS.md` (`app/node_modules/@readysetcloud/ui/AGENTS.md`) has the full contract.

## Repo layout

```
llm-eval-harness/
├── server/                   # FastAPI + Strands Agents SDK (uvicorn, :8000)
│   ├── nimbus/
│   │   ├── cli/              # the `nimbus` command line (main, commands, render)
│   │   ├── routers/          # health, models, tools, runs (+ evaluations), guardrails
│   │   ├── engine/           # run execution, streaming, fake model
│   │   ├── evals/            # determinism + grading engine, LLM-as-judge, cloud lane client
│   │   ├── guardrails/       # guardrail schemas/service/translator
│   │   ├── tools/            # @tool toolsets + registry.py
│   │   ├── store/            # SQLite + DynamoDB run/evaluation history
│   │   ├── worker/           # the Lambda eval worker entrypoint
│   │   └── auth.py           # Cognito bearer-token verification (deployed)
│   └── tests/
├── app/                      # React + TypeScript SPA (Vite, :3000)
│   ├── src/
│   ├── e2e/                  # Playwright, against the fake-model stack
│   └── .env.example
├── infra/                    # AWS SAM: table, eval worker, Lambda server, CloudFront, Cognito
│   ├── template.yaml
│   └── samconfig.toml
├── docs/                     # cli reference, contracts (cloud-evals, serverless-deploy), infra notes
├── scripts/                  # packaging scripts + the live smoke test
└── Makefile
```

## Testing

```bash
make test                   # app + server
cd app && npm test          # app only (vitest)
cd server && uv run pytest  # server only (pytest, fake model — no network)
make e2e                    # Playwright against the fake-model full stack
make validate-template      # cfn-lint on infra/template.yaml
make smoke                  # gated real-AWS smoke (RUN_LIVE_BEDROCK=1; costs money)
```

Every deploy also runs **`scripts/deploy_smoke.py`** against the URL it just published — anonymous
HTTP checks that the adapter boots and `/health` answers, that auth is switched on and the Cognito
pool is published, that a protected route returns the API's own 401 envelope, and that CloudFront
routes deep SPA links to `index.html`. It needs no credentials and spends nothing, which is why it
runs on every deploy rather than on request. Point it at anything yourself:

```bash
python3 scripts/deploy_smoke.py --url https://your-distribution.cloudfront.net
```

It exists because `nimbus serve` once shipped completely broken and passed every check in the
pipeline twice: unit tests, coverage and mutation testing all measure code as *imported*, and
nothing executed the thing and watched it answer.

Coverage gates are ratchets set at achieved numbers (`fail_under` in `server/pyproject.toml`,
`thresholds` in `app/vitest.config.ts`): raise them when coverage climbs, never lower them to make
a change pass. Mutation testing (Stryker for the app, mutmut for the server) runs as advisory CI
jobs.

## License

No license file is currently included in this repository.
