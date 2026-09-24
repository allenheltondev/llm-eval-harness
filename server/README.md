# Nimbus Server

FastAPI execution/eval engine for the harness, built on the
[Strands Agents SDK](https://strandsagents.com/). Executes runs against Bedrock, Anthropic, OpenAI
or Ollama, runs Python `@tool` handlers when a run names a toolset, runs determinism/grading
evaluations (`strands-agents-evals`), authors Bedrock Guardrails, and stores run/evaluation history
in SQLite (DynamoDB when deployed).

## Getting started

```bash
uv sync --dev
uv run uvicorn nimbus.main:app --reload --port 8000
```

Or, for zero network calls (scripted model + judge; test infrastructure, not a usage mode):

```bash
NIMBUS_FAKE_MODEL=1 uv run uvicorn nimbus.main:app --reload --port 8000
```

AWS credentials come from the standard credential chain (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, an
instance/task role, etc.).

| Env var                    | Default                            | Description |
| -------------------------- | ---------------------------------- | ----------- |
| `NIMBUS_AWS_REGION`        | `us-east-1`                        | AWS region for SDK calls |
| `NIMBUS_DB_PATH`           | `~/.local/share/nimbus/history.db` | SQLite database path |
| `NIMBUS_CORS_ORIGINS`      | `["http://localhost:3000"]`        | Allowed CORS origins (JSON list) |
| `NIMBUS_FAKE_MODEL`        | `false`                            | Use a fake model instead of live LLM |
| `NIMBUS_ANTHROPIC_API_KEY` | `None`                             | Anthropic API key (falls back to `ANTHROPIC_API_KEY`) |
| `NIMBUS_OPENAI_API_KEY`    | `None`                             | OpenAI API key (falls back to `OPENAI_API_KEY`) |
| `NIMBUS_OLLAMA_BASE_URL`   | `None`                             | Ollama server base URL, e.g. `http://localhost:11434` (falls back to `OLLAMA_HOST`) |
| `NIMBUS_EVAL_RUNTIME_ARN`  | `None`                             | AgentCore Runtime ARN for the cloud evaluation lane (stack output `EvalWorkerRuntimeArn`) |
| `NIMBUS_EVAL_TABLE`        | `None`                             | DynamoDB table for cloud-eval state and deployed history (stack output `TableName`) |
| `NIMBUS_HISTORY_BACKEND`   | `auto`                             | `sqlite` \| `dynamodb` \| `auto` (DynamoDB inside Lambda) |
| `NIMBUS_LOCAL_EVALS`       | `auto`                             | `on` \| `off` \| `auto` (off inside Lambda) |
| `NIMBUS_AUTH_USER_POOL_ID` | `None`                             | Cognito pool to verify bearer tokens against; with the client id, gates every route but `/health` |
| `NIMBUS_AUTH_CLIENT_ID`    | `None`                             | The pool's app client id |

## Model providers

Runs and evaluations select a provider with `provider` on the request body
(`"bedrock"` — the default — `"anthropic"`, `"openai"` or `"ollama"`); graders take
the same field, so an OpenAI judge can grade Bedrock runs. `GET /models` lists every
model across every configured provider, each entry tagged with the `source` to send
back as `provider`, alongside a `providers` block reporting which are configured
(also mirrored on `GET /health`).

A provider is "configured" when its credential resolves: the standard AWS chain for
Bedrock, an API key for Anthropic/OpenAI, a base URL for Ollama. Selecting an
unconfigured provider is a `400 provider_not_configured`. Guardrails are Bedrock-only
— pairing one with another provider is a `400 guardrail_requires_bedrock`.
`NIMBUS_FAKE_MODEL` short-circuits all of this, provider included.

## Toolsets

A run may name a toolset (`"toolset": "fraud-detection"`) — a list of Strands `@tool`
callables registered in `nimbus/tools/registry.py`. `GET /tools` lists them; an
unknown name is a `400 unknown_toolset`. Add a module next to `fraud_detection.py`
and register it to add your own.

## Endpoints

All routes are mounted under `/api/v1`. Every route except `/health` requires a
bearer token when a Cognito pool is configured (`nimbus/auth.py`).

| Router | Routes |
| --- | --- |
| `health` | `GET /health` |
| `models` | `GET /models` |
| `tools` | `GET /tools` |
| `runs` | `POST,GET /runs` (POST executes a run; `stream=true` returns NDJSON) · `GET,DELETE /runs/{id}` · `POST,GET /evaluations` · `GET /evaluations/{id}` · `GET /evaluations/{id}/events` (NDJSON) · `DELETE /evaluations/{id}` |
| `guardrails` | `GET,POST /guardrails` · `GET,PUT,DELETE /guardrails/{id}` · `GET,POST /guardrails/{id}/versions` |

## Testing & linting

```bash
uv run pytest --cov=nimbus        # fail_under in pyproject.toml is a ratchet
uv run ruff check .
```
