# Serverless deployment — shared contract

The harness has two ways to run, and the second is strictly additive:

- **Local-first (unchanged, the default)**: `make dev` — uvicorn + vite on your
  machine, SQLite history, evals in either lane. Nothing here changes.
- **Deployed (this document)**: everything on AWS is serverless. Lambda for the
  server, S3 + CloudFront for the SPA, DynamoDB for state, and a second Lambda
  for evaluation execution. **No long-lived containers anywhere**, and no
  exceptions to that.

This is the contract between the server-side work (history backend, lane
gating) and the infra work (Lambda hosting, packaging, CDN). Names here are
normative.

The infra half is built. See
[`serverless-deploy-infra.md`](./serverless-deploy-infra.md) for how it is
packaged and wired, with citations and the list of things only a real deploy
can prove — including two places where the implementation deliberately
diverges from the sketch below: CORS is the empty list rather than the
CloudFront domain (same-origin needs none, and deriving it would be circular),
and SPA fallback is a per-behaviour CloudFront Function rather than
distribution-wide `CustomErrorResponses` (which would corrupt the API's own
403/404s).

## Compute shape

| Piece | Deployed as |
|---|---|
| FastAPI server | One Lambda function (zip, arm64, Python 3.12) running the UNCHANGED
  `nimbus.main:app` behind the **AWS Lambda Web Adapter** layer, exposed via a
  **Function URL with `InvokeMode: RESPONSE_STREAM`** so `POST /runs` and the
  evaluation event streams keep their NDJSON semantics. `AWS_LWA_INVOKE_MODE=response_stream`. |
| React SPA | Static build in S3 behind CloudFront. `VITE_API_URL` baked at build
  time pointing at the server's URL. |
| Eval execution | A worker Lambda, invoked asynchronously (`docs/cloud-evals-infra.md`). |
| History | DynamoDB (below). SQLite never runs in Lambda. |

Timeout: server Lambda 900s (a streaming run must finish inside one invocation).
Memory 1024MB default, parameterized.

## History backend

`NIMBUS_HISTORY_BACKEND: "sqlite" | "dynamodb" | "auto"` (default `auto`:
dynamodb when running inside Lambda — detect via `AWS_LAMBDA_FUNCTION_NAME` —
else sqlite).

The DynamoDB backend implements the SAME repository surface as
`nimbus/store/history.py` (create_run/update_run/get_run/delete_run/
list_runs/iter_runs_export/create_evaluation/update_evaluation/get_evaluation/
list_evaluations, identical signatures and cursor semantics) against the
stack's `EvalTable`, REUSING the cloud-eval item shapes verbatim
(docs/cloud-evals.md): `RUN#{id}/META` with `GSI1PK="RUN", GSI1SK={ts}`,
`EVAL#{id}/META` with `GSI1PK="EVAL"`. One deliberate consequence: deployed-server
runs and cloud-lane worker runs land in the same partitions and read back
through one code path. Filters (model_id/status/since) apply
post-page like the existing cloud listing. TTL: history is kept for
`HistoryRetentionDays` (default `0`, forever) — see docs/cloud-evals.md.

Evaluations in the deployed server are cloud-lane only:
- `POST /evaluations` with `execution: "local"` → 400
  `{"error": {"code": "local_lane_unavailable", ...}}` when the local lane is off.
- Local lane availability is a setting: `NIMBUS_LOCAL_EVALS: bool` default
  `auto` semantics — disabled when running in Lambda, enabled otherwise.
- Health gains `"local_evals": {"available": bool}`; the UI's "This machine"
  option disables (with hint) when false, and the default flips to cloud.

## Auth (deployed)

The deployed server requires sign-in; the gate is the application, not the
infrastructure. The Function URL keeps `AuthType: NONE` and CloudFront stays
open (neither can be closed for a browser that POSTs — CloudFront OAC for
function URLs needs a viewer-computed body hash; `serverless-deploy-infra.md`
"Auth"). CloudFront fronts BOTH the SPA (S3 origin) and the server (Function
URL origin, path `/api/*`) so the browser sees one origin — no CORS in
production, and `VITE_API_URL` stays relative.

The shape follows `readysetcloud/rsc-core` (Cognito user pool + app client in
the SAM template, the browser calling `cognito-idp` directly, no Hosted UI):

- **Template** (`infra/template.yaml`, condition `DeployServer`): `UserPool`
  (email usernames, admin-only user creation, `${AWS::StackName}-users`) and
  `UserPoolClient` (`USER_PASSWORD_AUTH` + `REFRESH_TOKEN_AUTH`, no secret,
  1h id/access tokens, 30-day refresh). Outputs `UserPoolId`,
  `UserPoolClientId`. The server function gets
  `NIMBUS_AUTH_USER_POOL_ID` / `NIMBUS_AUTH_CLIENT_ID`.
- **Server** (`nimbus/auth.py`): with both settings present, every router
  except `/health` carries a `require_auth` dependency. It accepts
  `Authorization: Bearer <jwt>` where the JWT is an ID token (`aud` = client)
  or an access token (`client_id` = client), RS256-signed by the pool's JWKS
  (fetched once per process, re-fetched on an unknown `kid`), with `iss`,
  `exp` and `token_use` checked. Failure is `401 {"error": {"code":
  "unauthorized"}}`; an unreachable JWKS is `502 upstream_error`. Neither
  setting present (every local run, the E2E suite) means no gate.
- **Health**: `GET /health` stays open and gains
  `"auth": {"required": false}` or `{"required": true, "provider":
  "cognito", "region", "user_pool_id", "client_id"}` — the SPA's only source
  of auth configuration, so nothing is baked in at build time.
- **SPA** (`app/src/auth/`): `AuthGate` reads `/health`; when auth is
  required it configures the core, installs a token provider on the HTTP
  layer (`setAuthTokenProvider`, so every JSON request and NDJSON stream
  carries the bearer header) and renders `LoginPage` until a session exists.
  A `401` from the API drops the session and returns to sign-in with a
  notice. Sign-in, the `NEW_PASSWORD_REQUIRED` first-login step, forgot /
  reset password, silent refresh and revoke-on-sign-out are the rsc-core
  core, minus sign-up and the cross-subdomain cookie bridge.
- **Users**: `make create-user EMAIL=...` (`admin-create-user` against the
  stack's `UserPoolId` output). Cognito emails a temporary password.

## Deploy flow

- `make deploy` = `make deploy-backend` (package the server zip and the eval
  worker zip — uv, arm64 wheels, manylinux_2_28 — upload both, deploy the SAM
  stack: table + worker runtime + server function + LWA layer + Function URL +
  S3 bucket + CloudFront + Cognito) then `make deploy-frontend` (build the SPA
  with `VITE_API_URL=/`, sync to S3, invalidate). Idempotent; prints the final
  URL. CI runs the same two targets as separate jobs.
- Stack outputs added: `ServerFunctionUrl`, `AppUrl` (CloudFront domain),
  `AppBucket`.
- IAM for the server function role: Bedrock invoke + guardrails, DynamoDB on the
  table, `lambda:InvokeFunction` on the worker function. The
  template injects NIMBUS_EVAL_TABLE / EVAL_FUNCTION_NAME / AUTH_* directly
  from `!Ref`/`!GetAtt`; there is no runtime discovery of anything.

### The rename to Nimbus

CloudFormation applies a function's configuration and its code in separate
calls, and requests are served in between, so no single deploy may change
something the code on the other side of that gap still needs. The rename from
`evalharness` to `nimbus` was therefore spread over three releases:

1. every application setting was given under both names, `NIMBUS_*` and
   `EVALHARNESS_*` with the same value, so old code never lost its settings —
   above all the auth pair, without which it would have served every route
   unauthenticated through the public Function URL — and the worker's `Handler`
   kept its old name, served by a shim in the worker zip;
2. the `EVALHARNESS_*` variables were dropped and the `Handler` moved to
   `nimbus.worker.lambda_app.handler`, with the shim still shipped in case the
   new code landed before the new handler;
3. the shim was deleted. That release is only safe once step 2 has
   *succeeded* on the stack, so `make deploy-backend` now first runs
   `scripts/check-deploy-prerequisites.sh`, which reads the live worker's
   `Handler` and stops the deploy unless it is already
   `nimbus.worker.lambda_app.handler`. The workflow's concurrency group orders
   deploys but would still run this one after a failed step 2. The check fails
   closed: it needs `cloudformation:DescribeStackResource` and
   `lambda:GetFunctionConfiguration`, and a denial stops the deploy with the
   reason.

The same pattern applies to any future rename of a setting or a handler: add
the new name alongside the old, switch over, then remove the old — one deploy
each. The CLI and a local server still read `EVALHARNESS_*` as a fallback on
people's own machines; that is unrelated to the deployed stack.

## Out of scope (documented, not built)

Custom domains, WAF, self sign-up / per-user data isolation (the pool gates
access; the data behind it is still one shared workspace), provider API keys
for non-Bedrock models in the deployed server (same Bedrock-only caveat as
the worker; add SSM-parameter wiring later if wanted).
