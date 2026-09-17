# LLM Eval Harness

An evaluation harness for LLM prompts and agents, built on the
[Strands Agents SDK](https://strandsagents.com/). Run a prompt against a model with streaming
output and live tool-use, then grade the results with an LLM-as-judge determinism evaluation, and
keep the full run and evaluation history for comparison and export.

Evaluations are the product. A run is one execution of a prompt; an evaluation runs it `n` times
(or grades runs you already have) and scores the batch with `strands-agents-evals`. Everything
runs local-first; a serverless deployment (Lambda + CloudFront + Cognito) is optional.

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
                              |  AgentCore eval worker      |
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
- **`infra/`** — one AWS SAM template: the DynamoDB table, the AgentCore Runtime worker for the
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
EVALHARNESS_FAKE_MODEL=1 make dev
```

### Real mode — live model calls

```bash
AWS_PROFILE=your-profile make dev          # Bedrock
OLLAMA_HOST=localhost:11434 make dev       # or a local model, no cloud account at all
```

The app opens at `http://localhost:3000` and talks to the server at `http://localhost:8000`.
History lives in `server/data/evalharness.db`.

## Runs and toolsets

`POST /api/v1/runs` executes one prompt (`system_prompt`, `user_prompt`, `model_id`, `provider`,
inference settings, optional guardrail) and streams the result as NDJSON, or returns the finished
run with `stream: false`.

A run may name a **toolset** (`"toolset": "fraud-detection"`), a list of Python `@tool` handlers
the model can call during the run. `GET /api/v1/tools` lists what is registered. One example
ships; add your own next to it:

```python
# server/evalharness/tools/support.py
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
# server/evalharness/tools/registry.py
_REGISTRY["support"] = [support.escalate_ticket]
```

## Evaluations

`POST /api/v1/evaluations` runs one of two kinds of evaluation, both graded by an LLM-as-judge
(`strands-agents-evals`):

- **`determinism`** — runs the same `run_config` `n` times (2–25, default 10) and grades the batch
  for response consistency.
- **`grade`** — grades a set of already-executed runs (`run_ids`) against a rubric.

The grader model, rubric, and system prompt are all configurable per request
(`grader.model_id`, defaults to `amazon.nova-pro-v1:0`; `grader.system_prompt`; `rubric`). Progress
streams as NDJSON from `GET /api/v1/evaluations/{id}/events`; results and history live alongside
runs in the server's SQLite database.

### Execution lanes: local vs cloud

Evaluations run in one of two lanes, chosen per launch with the **Run location** toggle in the
Evals tab (`execution: "local" | "cloud"` on the API):

- **This machine** (default) — executed in-process by the server; history stays in local SQLite.
  Nothing leaves your machine except the model calls themselves.
- **Cloud — persisted** — executed by a worker hosted on Amazon Bedrock AgentCore Runtime; job
  state, progress events, and every run record are persisted to the stack's DynamoDB table, so
  evaluations survive laptop/server restarts and are reviewable from any machine pointed at the
  same stack. Cloud evaluations appear under the **Cloud** filters in the Evals and History tabs.

The cloud lane is part of the stack: `make deploy-backend` packages the Python worker as an
AgentCore CodeZip artifact alongside the server zip and deploys both. It prints the two values a
local server needs to use the deployed lane:

```bash
EVALHARNESS_EVAL_RUNTIME_ARN=arn:aws:bedrock-agentcore:...   # stack output EvalWorkerRuntimeArn
EVALHARNESS_EVAL_TABLE=llm-eval-harness-EvalTable-...         # stack output TableName
```

The UI disables the cloud option until `GET /health` reports `cloud_evals.configured`. Full design
and item shapes: `docs/cloud-evals.md`; infrastructure notes and first-deploy verification list:
`docs/cloud-evals-infra.md`.

## Guardrails

`server/evalharness/routers/guardrails.py` authors AWS Bedrock Guardrails directly: create/update
operate on a mutable `DRAFT` working copy, `POST /guardrails/{id}/versions` publishes the current
draft as a new immutable numbered version, and any version (including `DRAFT`) can be applied to a
run. `GET /guardrails/{id}/versions` lists the full version history for a guardrail. Bedrock-only.

## Deploy to AWS (serverless)

Optional. When you want the harness on the internet, it is serverless end to end: **no containers
anywhere**, one Lambda for the FastAPI server (unchanged, behind the
[AWS Lambda Web Adapter](https://github.com/aws/aws-lambda-web-adapter)), S3 + CloudFront for the
SPA, DynamoDB for history, and the AgentCore Runtime for evaluations.

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

The deployed app requires sign-in. The stack creates a **Cognito user pool** and app client, the
server verifies a bearer token from that pool on every route except `/api/v1/health`, and the SPA
shows a sign-in screen until it has one. Nothing is baked into the build: the SPA learns from
`/health` whether sign-in is required and which pool to use, so the same `app/dist` works
locally (no pool, no gate) and deployed (pool, gate).

The pool is **invitation-only** — there is no sign-up form, because an account here can spend
your model budget. Create each user from the CLI; Cognito emails them a temporary password and
the app walks them through choosing a real one on first sign-in:

```bash
make create-user EMAIL=you@example.com          # STACK_NAME=... for another stack
```

Sign-in talks to `cognito-idp.<region>.amazonaws.com` straight from the browser (no Hosted UI,
no redirect); the resulting ID token is sent as `Authorization: Bearer` and refreshed silently
for as long as the refresh token lasts (30 days). Sign out from the header. This is the same
pattern as [`readysetcloud/rsc-core`](https://github.com/readysetcloud/rsc-core)'s
`@readysetcloud/ui/auth`, trimmed to what this app needs.

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
bucket **and** the SPA bucket, including `s3:DeleteObject` for the `--delete` sync), Lambda (and
the Lambda Web Adapter layer's `lambda:GetLayerVersion`), IAM (role creation and `iam:PassRole`),
DynamoDB, CloudFront (distributions, origin access controls, functions, and
`cloudfront:CreateInvalidation`, which cannot be resource-scoped), Cognito user pools, and Bedrock
AgentCore runtimes (`bedrock-agentcore:*` on runtimes plus `iam:CreateServiceLinkedRole` for its
first use). `make create-user` additionally needs `cognito-idp:AdminCreateUser` on the pool, for
whoever runs it.

One sharp edge: `ServerArtifactKey` and `EvalWorkerArtifactKey` are CloudFormation parameters with
empty defaults, and an empty value deletes the corresponding resource. `make deploy-backend`
always passes both, so use it rather than a bare `sam deploy` once anything is deployed.

Details — the adapter layer, packaging and artifact size, the CloudFront origin/behaviour setup,
exact IAM, and the list of things only a real deploy can prove — are in
[`docs/serverless-deploy.md`](docs/serverless-deploy.md) (the contract) and
[`docs/serverless-deploy-infra.md`](docs/serverless-deploy-infra.md) (the build).

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
| `make create-user EMAIL=...` | Invites a user to the deployed stack's Cognito pool ([Sign-in](#sign-in-cognito)); Cognito emails them a temporary password |

`make dev` runs both processes as background jobs of one recipe with a `trap ... EXIT INT TERM`
so `Ctrl-C` (or any exit) tears down both — no orphaned `uvicorn`/`vite` process left behind. If
that ever proves flaky in your shell, use the two `dev-server` / `dev-app` targets in separate
terminals instead; they run the exact same commands.

## Configuration

### Server (`server/`, env vars prefixed `EVALHARNESS_`)

| Env var | Default | Description |
| --- | --- | --- |
| `EVALHARNESS_AWS_REGION` | `us-east-1` | AWS region for Bedrock/Guardrails/DynamoDB calls |
| `EVALHARNESS_DB_PATH` | `./data/evalharness.db` | SQLite path for run/evaluation history |
| `EVALHARNESS_CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed CORS origins (JSON list) |
| `EVALHARNESS_FAKE_MODEL` | `false` | Use the scripted fake model + judge instead of a real provider (test infrastructure) |
| `EVALHARNESS_ANTHROPIC_API_KEY` | *(unset)* | Anthropic API key — enables the `anthropic` provider (falls back to `ANTHROPIC_API_KEY`) |
| `EVALHARNESS_OPENAI_API_KEY` | *(unset)* | OpenAI API key — enables the `openai` provider (falls back to `OPENAI_API_KEY`) |
| `EVALHARNESS_OLLAMA_BASE_URL` | *(unset)* | Ollama server base URL, e.g. `http://localhost:11434` — enables the `ollama` provider (falls back to `OLLAMA_HOST`) |
| `EVALHARNESS_EVAL_RUNTIME_ARN` | *(unset)* | AgentCore runtime ARN of the eval worker (stack output `EvalWorkerRuntimeArn`). With `EVAL_TABLE`, enables the cloud lane |
| `EVALHARNESS_EVAL_TABLE` | *(unset)* | DynamoDB table for cloud-eval state and deployed history (stack output `TableName`) |
| `EVALHARNESS_HISTORY_BACKEND` | `auto` | `sqlite` \| `dynamodb` \| `auto` (DynamoDB inside Lambda, SQLite elsewhere) |
| `EVALHARNESS_LOCAL_EVALS` | `auto` | `on` \| `off` \| `auto` (off inside Lambda) — whether evaluations may run in this process |
| `EVALHARNESS_AUTH_USER_POOL_ID` | *(unset)* | Cognito user pool to verify bearer tokens against. With `EVALHARNESS_AUTH_CLIENT_ID`, every route but `/health` requires a token; unset locally means no gate. The deployed stack injects both |
| `EVALHARNESS_AUTH_CLIENT_ID` | *(unset)* | The pool's app client id — what the SPA signs in with and what every accepted token's `aud`/`client_id` must equal |

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

## Repo layout

```
llm-eval-harness/
├── server/                   # FastAPI + Strands Agents SDK (uvicorn, :8000)
│   ├── evalharness/
│   │   ├── routers/          # health, models, tools, runs (+ evaluations), guardrails
│   │   ├── engine/           # run execution, streaming, fake model
│   │   ├── evals/            # determinism + grading engine, LLM-as-judge, cloud lane client
│   │   ├── guardrails/       # guardrail schemas/service/translator
│   │   ├── tools/            # @tool toolsets + registry.py
│   │   ├── store/            # SQLite + DynamoDB run/evaluation history
│   │   ├── worker/           # the AgentCore eval worker entrypoint
│   │   └── auth.py           # Cognito bearer-token verification (deployed)
│   └── tests/
├── app/                      # React + TypeScript SPA (Vite, :3000)
│   ├── src/
│   ├── e2e/                  # Playwright, against the fake-model stack
│   └── .env.example
├── infra/                    # AWS SAM: table, eval worker, Lambda server, CloudFront, Cognito
│   ├── template.yaml
│   └── samconfig.toml
├── docs/                     # contracts (cloud-evals, serverless-deploy) and infra notes
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
```

Coverage gates are ratchets set at achieved numbers (`fail_under` in `server/pyproject.toml`,
`thresholds` in `app/vitest.config.ts`): raise them when coverage climbs, never lower them to make
a change pass. Mutation testing (Stryker for the app, mutmut for the server) runs as advisory CI
jobs.

## License

No license file is currently included in this repository.
