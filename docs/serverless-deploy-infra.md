# Deployed server + SPA — infrastructure notes

Companion to [`serverless-deploy.md`](./serverless-deploy.md), which is the
normative contract between the server-side work and this. That document says
*what* the deployed shape is; this one covers **how it is packaged and wired,
where each fact came from, and what about it only a real deploy can prove.**

Everything here concerns `scripts/package-server.sh`, the `ServerFunction` /
`AppBucket` / `AppDistribution` half of `infra/template.yaml`, and `make deploy`.

## Where the facts came from

Same two-tier split as [`cloud-evals-infra.md`](./cloud-evals-infra.md),
because the sources do not deserve equal confidence.

**Read directly, as source** — the strongest evidence available without an AWS
account:

- [`aws/aws-lambda-web-adapter` README](https://github.com/aws/aws-lambda-web-adapter)
  (fetched raw from `main`) — the layer ARNs, the full configuration
  environment-variable table with defaults, and the zip-package setup. Note the
  repository moved from `awslabs/` to `aws/`; both paths still resolve.
- [`examples/fastapi-response-streaming-zip/template.yaml`](https://github.com/aws/aws-lambda-web-adapter/blob/main/examples/fastapi-response-streaming-zip/template.yaml)
  and its
  [`app/run.sh`](https://github.com/aws/aws-lambda-web-adapter/blob/main/examples/fastapi-response-streaming-zip/app/run.sh)
  — an AWS-maintained SAM template doing exactly this: FastAPI, zip package,
  `Handler: run.sh`, adapter layer, `AuthType: NONE` + `InvokeMode:
  RESPONSE_STREAM`. This is what makes the shape a fact rather than a hope.
- `/workspace/readysetcloud/rsc-core/template.yaml` — a working CloudFront
  distribution in front of a non-S3 origin, and the source of the two managed
  policy ids used below. Its `CognitoUserPool` / `CognitoUserPoolClient` are
  the model for this stack's `UserPool` / `UserPoolClient`, and
  `ui/src/auth/core.ts` (the published `@readysetcloud/ui/auth`) is the model
  for `app/src/auth/core.ts` — the `cognito-idp` calls, the session document
  and the refresh/revoke behaviour were ported from it, not designed here.
- This repository's own source (`server/evalharness/**`, `app/src/api/http.ts`)
  for every claim about what the application does.

**Second-hand, from AWS documentation via search** — `docs.aws.amazon.com` is
blocked by this environment's egress proxy, so these were read as search
summaries, not fetched pages. Strong hints, not verified:

- [SAM `FunctionUrlConfig`](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-function-functionurlconfig.html)
  — `InvokeMode` is `BUFFERED` (default) or `RESPONSE_STREAM`.
- [Invoking a response streaming enabled function using function URLs](https://docs.aws.amazon.com/lambda/latest/dg/config-rs-invoke-furls.html)
  and [Introducing AWS Lambda response streaming](https://aws.amazon.com/blogs/compute/introducing-aws-lambda-response-streaming/)
  — the payload ceiling and the bandwidth shape.
- [Restrict access to an AWS Lambda function URL origin](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-lambda.html)
  — CloudFront OAC for Lambda function URLs, and its POST/PUT body-hash
  requirement.
- [Control origin requests with a policy](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/controlling-origin-requests.html)
  — the `Authorization` header rule (forwarded when the cache policy names
  it, or under the managed `CachingDisabled` policy via the origin request
  policy). Recalled, not fetched: the page is behind the same proxy block.
- [Verifying a JSON web token](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html)
  — the JWKS URL shape and the `iss` / `aud` / `client_id` / `token_use` /
  `exp` checks `evalharness/auth.py` implements. Same caveat.

Claims below are tagged **[lwa]**, **[rsc]**, **[repo]**, **[docs]**, or
**[measured]** (something actually run on this branch).

---

## The adapter, and why the application is untouched

`evalharness.main:app` runs on Lambda **unchanged** — no handler function, no
Mangum, no ASGI-to-Lambda shim anywhere in `server/`. The AWS Lambda Web
Adapter layer does the translation out of process:

1. `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap` makes the Lambda runtime exec the
   layer's bootstrap instead of the normal Python runtime **[lwa]**.
2. That bootstrap starts the adapter, which execs the function's `Handler` —
   here `run.sh`, a file at the root of the zip **[lwa]**.
3. `run.sh` starts uvicorn on `$PORT`.
4. The adapter polls the Lambda Runtime API, converts each invocation into an
   ordinary HTTP request against `127.0.0.1:$PORT`, and converts the response
   back.

The environment variables that matter, with the defaults they override
**[lwa]**:

| Variable | Value here | Adapter default |
|---|---|---|
| `AWS_LAMBDA_EXEC_WRAPPER` | `/opt/bootstrap` | — (required for zip packages) |
| `AWS_LWA_INVOKE_MODE` | `response_stream` | `buffered` |
| `PORT` | `8080` | `AWS_LWA_PORT`, then `8080` |
| `AWS_LWA_READINESS_CHECK_PATH` | *(unset)* | `/` |
| `AWS_LWA_READINESS_CHECK_HEALTHY_STATUS` | *(unset)* | `100-499` |

### Readiness is left at the default, on purpose

The obvious move is `AWS_LWA_READINESS_CHECK_PATH=/api/v1/health`. That is the
wrong call here. The default path `/` combined with the default healthy range
`100-499` means "the ASGI app answered at all" — FastAPI returns 404 there
(nothing is mounted at `/`), 404 is inside 100-499, and the adapter proceeds
**[lwa]** **[repo]**. That is exactly the signal wanted, and it costs nothing.

`/api/v1/health`, by contrast, probes the config store over HTTP and every
model provider before returning **[repo]** — making every cold start pay for
two round trips before the first real request is even admitted, and coupling
boot to the reachability of a service the server can perfectly well run
without.

### `run.sh`

Generated by `scripts/package-server.sh` (edit the script, not the artifact):

```bash
#!/bin/bash
set -euo pipefail
PATH="${PATH}:${LAMBDA_TASK_ROOT}/bin" \
PYTHONPATH="${PYTHONPATH:-}:${LAMBDA_TASK_ROOT}:/opt/python:${LAMBDA_RUNTIME_DIR:-}" \
  exec python3 -m uvicorn \
    --host 127.0.0.1 \
    --port "${AWS_LWA_PORT:-${PORT:-8080}}" \
    evalharness.main:app
```

Two deliberate deviations from AWS's example **[lwa]**:

- **`python3`, not `python`.** The example uses `python`; every Lambda Python
  runtime ships `python3` under that exact name, so this removes one thing that
  can differ between runtime images.
- **`--host 127.0.0.1` stated explicitly.** It is uvicorn's default and it is
  what the adapter connects to, but a future uvicorn default change should not
  be able to break the deploy silently.

`exec` is load-bearing: the adapter's process tree expects uvicorn to *be* the
handler process, not a child of a shell that would swallow its signals.

The file must be **executable inside the zip**. The packaging script `chmod
0755`s it and zips with `zip -X`, which drops extra attributes but keeps unix
permission bits — verified on the built artifact **[measured]**:

```
-rwxr-xr-x  3.0 unx      623 t- defN 80-Jan-01 00:00 run.sh
```

### Layer ARN

```
arn:<partition>:lambda:<region>:753240598075:layer:LambdaAdapterLayerArm64:28
```

Account `753240598075` publishes the layer into every commercial region, so the
region is a `!Sub` substitution rather than a lookup table **[lwa]**. It is
still parameterized two ways, because a hardcoded layer ARN is a classic
stale-forever dependency:

- `LambdaAdapterLayerVersion` (default `28`, the current published version
  **[lwa]**) — the knob anyone actually turns.
- `LambdaAdapterLayerArn` (default empty) — a full override for a partition or
  private mirror where that account does not publish. The upstream README notes
  the China partition is not deployed to.

`Arm64`, not `X86`, because the function is arm64 (inherited from the
template's `Globals`) and the artifact's wheels are cross-compiled for
`aarch64-manylinux_2_28`.

---

## Streaming

`AWS_LWA_INVOKE_MODE=response_stream` on the adapter and `InvokeMode:
RESPONSE_STREAM` on the Function URL have to agree; either one alone buffers.
SAM supports `InvokeMode` directly on `FunctionUrlConfig` **[docs]**, and the
AWS-maintained FastAPI example sets exactly this pair **[lwa]**.

Limits worth knowing:

| Limit | Value | Source |
|---|---|---|
| Streaming response payload | 200 MB (soft, raisable; was 20 MB at launch) | **[docs]** |
| Unthrottled prefix | first 6 MB | **[docs]** |
| Bandwidth past 6 MB | ~16 Mbps (2 MB/s) | **[docs]** |
| Function timeout | 900 s (Lambda max) | — |
| CloudFront origin response / inter-packet timeout | 60 s (set explicitly; 30 s default, 60 s max without a quota increase) | **[docs]** |

That last row is the one with teeth. CloudFront's origin response timeout is
also its *inter-packet* timeout on a streaming response, so a run that goes
more than 60 seconds between NDJSON events will have its connection dropped by
CloudFront even though the Lambda is happily still running. `OriginReadTimeout:
60` buys the maximum available without a quota request. If long tool-using runs
turn out to exceed that, the fix is a periodic heartbeat event in the run
stream, not an infrastructure change.

---

## The artifact

`scripts/package-server.sh` is `package-eval-worker.sh`'s sibling and shares
its two hard-won rules: cross-compiled `aarch64-manylinux_2_28` wheels with
`--only-binary=:all:` as a loud failure mode, and a **content-hashed S3 key**
(`server/<sha256[:16]>.zip`). The second is rsc-core's `AgentArtifactKey`
lesson again: CloudFormation only updates a function when a property it can
*see* changes, and for an S3-sourced function that property is the key. Upload
new bytes under a static key and CloudFormation correctly concludes nothing
changed.

Differences from the worker's packaging:

- The root entry is `run.sh`, executable, not a Python module.
- **The same dependency set.** Both artifacts now export the server's locked
  runtime dependencies with no extras — the eval worker stopped needing
  anything of its own when it moved to Lambda, and this server only ever
  *invokes* it, through boto3.
- Dev dependencies are excluded (`--no-dev`), same as the worker.
- uv's `.lock` marker file is removed from the staging tree — build-machine
  state, not code, and it would otherwise perturb the content hash.

### Artifact size

Lambda's ceiling is **250 MB unzipped**, counted across the function *and*
every layer it attaches — the same ceiling the worker artifact now lives under, with no slack
to be relaxed about. Measured on this branch **[measured]**:

| | zipped | unzipped |
|---|---|---|
| Everything the lockfile resolves | 56 MB | 199 MB |
| After pruning (what ships) | **46 MB** | **157 MB** |

199 MB would have fit, barely, but with ~50 MB of headroom for a stack whose
largest dependency (`botocore`, 28 MB) grows monthly. So four packages are
pruned by default:

| Pruned | Size | Why it is safe |
|---|---|---|
| `sympy` | 19 MB | |
| `pillow.libs` | 16 MB | |
| `PIL` | 7 MB | |
| `mpmath` | 2 MB | (sympy's own dependency) |

All four arrive via `strands-agents-tools`, and the only modules that import
them are `strands_tools/calculator.py`, `strands_tools/image_reader.py` and
`strands_tools/use_computer.py` **[measured]** — none of which any
`evalharness` module imports, directly or dynamically (the package contains no
`importlib.import_module` / `__import__` call at all **[measured]**).
`strands_evals`, which *is* used, does not reference `strands_tools` anywhere
**[measured]**.

The consequence to know about: if a future change wants those three
`strands_tools` modules, they will `ImportError` in Lambda and work fine
locally. Set `SERVER_PRUNE=0` to build unpruned, and re-check the size. The
script hard-fails the build above 240 MB unzipped rather than letting the
deploy discover it.

Uploading from S3 rather than inline also sidesteps the 50 MB direct-upload
limit, which 46 MB is uncomfortably close to.

---

## Environment wiring

Everything the server needs is injected from the template; the server has no
runtime discovery of any kind, and the function role has no read access to
CloudFormation or API Gateway.

| Env var | Value | Why |
|---|---|---|
| `EVALHARNESS_EVAL_TABLE` | `!Ref EvalTable` | History backend and cloud-eval state |
| `EVALHARNESS_EVAL_FUNCTION_NAME` | `!Ref EvalWorkerFunction`, or absent when the worker is not deployed | The cloud evaluation lane |
| `EVALHARNESS_AUTH_USER_POOL_ID` / `EVALHARNESS_AUTH_CLIENT_ID` | `!Ref UserPool` / `!Ref UserPoolClient` | The bearer-token gate ("Auth" below) |
| `EVALHARNESS_DB_PATH` | `/tmp/evalharness.db` | `/var/task` is read-only; the run engine opens a SQLite file for scratch even under the DynamoDB history backend |
| `EVALHARNESS_HISTORY_BACKEND` | `dynamodb` | Explicit rather than relying on `auto`'s `AWS_LAMBDA_FUNCTION_NAME` detection |
| `EVALHARNESS_LOCAL_EVALS` | `off` | Same reasoning |
| `EVALHARNESS_AWS_REGION` | `!Ref AWS::Region` | |
| `EVALHARNESS_CORS_ORIGINS` | `[]` (parameter `ServerCorsOrigins`) | See below |

### CORS: the empty list is the correct value

The SPA and the API share one CloudFront origin, so **nothing the browser does
is cross-origin** and no `Access-Control-Allow-Origin` header is needed at all.
An empty list is therefore not a compromise — it is tighter than naming the
CloudFront domain, because it also denies any *other* page that guesses the
Function URL.

Naming the CloudFront domain would additionally have been impossible to express:
the distribution's origin is the Function URL, so a CORS value derived from
`!GetAtt AppDistribution.DomainName` on the function's own environment is a
circular dependency.

`ServerCorsOrigins` exists for the one case that does need it — pointing a
local `vite` at the deployed server — e.g.
`--parameter-overrides 'ServerCorsOrigins=["http://localhost:3000"]'`.

---

## CloudFront: one distribution, two origins

```
                    ┌───────────────────────────────┐
   browser ────────▶│  AppDistribution (CloudFront) │
                    ├───────────────────────────────┤
                    │  default  → S3 (OAC, private) │  SPA
                    │  /api/*   → Lambda Function URL│  FastAPI
                    └───────────────────────────────┘
```

Mechanics, following rsc-core's Function-URL-behind-CloudFront precedent
**[rsc]**:

- **Origin domain.** `!GetAtt ServerFunctionUrl.FunctionUrl` returns
  `https://<id>.lambda-url.<region>.on.aws/`; CloudFront wants the bare host,
  which is `!Select [2, !Split ['/', ...]]`.
- **`CachingDisabled`** (`4135ea2d-6df8-44a3-9df3-4b5a84be39ad`) on `/api/*` —
  an NDJSON run stream is never cacheable **[rsc]**.
- **`AllViewerExceptHostHeader`** (`b689b0a8-53d0-40ab-baf2-68738e2966ac`) —
  forwards every viewer header, cookie and query string, while letting
  CloudFront set `Host` to the Function URL's own domain. Lambda rejects a
  request whose `Host` does not match, so the plain `AllViewer` policy would
  break it **[rsc]**.
- **No `OriginPath` rewriting and no `AWS_LWA_REMOVE_BASE_PATH`.** The FastAPI
  routers mount at `/api/v1` **[repo]**, so `/api/*` matches the real paths and
  the origin receives them unchanged. The path prefix is a genuine coincidence
  of design, and a lucky one.
- **`CachingOptimized`** (`658327ea-f89d-4fab-a63d-7e88639e58f6`) on the
  default behaviour. Vite emits content-hashed asset filenames; `index.html` is
  covered by the invalidation `make deploy` issues.

### SPA fallback is a CloudFront Function, not `CustomErrorResponses`

The standard recipe is `CustomErrorResponses: 403/404 → /index.html (200)`.
**Do not use it here.** `CustomErrorResponses` is a distribution-level
property — it is not scoped to a cache behaviour — so it would also rewrite the
API's own 404s (`GET /api/v1/runs/{unknown-id}`) into a 200 carrying the HTML
shell. The app's error layer would see a 200 whose body is not JSON and raise
`invalid_response` instead of surfacing a not-found **[repo]**.

A CloudFront Function *is* per-behaviour. `SpaRouterFunction` is attached to
the default behaviour only, so `/api/*` never touches it:

```js
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  var last = uri.substring(uri.lastIndexOf('/') + 1);
  if (last.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
```

Deliberately ES5-only (`indexOf`/`substring`, no `includes`/`endsWith`) so it is
valid under either CloudFront Functions runtime. `/history` and
`/scenarios/abc` rewrite to `/index.html`; `/assets/index-abc123.js` and
`/favicon.ico` pass through untouched.

One consequence: FastAPI's `/docs` and `/openapi.json` are *not* reachable
through CloudFront — they are not under `/api/*`, so they hit the SPA origin
and get the app shell. Use the `ServerFunctionUrl` output directly for those.

### The SPA's API base URL

`app/src/api/http.ts` needed **no change**. Its `baseUrl()` treats a blank
`VITE_API_URL` as unset and falls back to `http://localhost:8000`, but it
strips trailing slashes before use — so **`VITE_API_URL="/"` resolves to an
empty base**, and `apiUrl()` emits relative paths like `/api/v1/runs`
**[measured]**:

| `VITE_API_URL` | `apiUrl('/runs')` |
|---|---|
| *(unset)* | `http://localhost:8000/api/v1/runs` |
| `""` | `http://localhost:8000/api/v1/runs` ← **not** what you want |
| `"/"` | `/api/v1/runs` ✅ |
| `"/api/v1"` | `/api/v1/runs` ✅ |
| `"https://d123.cloudfront.net"` | `https://d123.cloudfront.net/api/v1/runs` |

`make deploy` therefore builds with `VITE_API_URL=/` (the `DEPLOY_API_URL`
variable). Both `http.ts` and `stream.ts` route through `apiUrl()`, so this
covers the entire API surface including the NDJSON streams **[repo]**.

---

## Auth: where the gate is, and where it is not

Stated plainly, because it is the thing most likely to be misremembered.

**The gate is the application.** With `EVALHARNESS_AUTH_USER_POOL_ID` and
`EVALHARNESS_AUTH_CLIENT_ID` set — the template injects both from its own
`UserPool` / `UserPoolClient` — `evalharness.auth.require_auth` sits on every
router except `/health`. A request without `Authorization: Bearer <jwt>`, or
with a token the pool did not sign for this client, gets `401
{"error": {"code": "unauthorized"}}` before its body is even parsed
**[repo]**. Because the check is in the ASGI app, it is identical through
both doors: the Function URL and the CloudFront distribution.

**Not gated, on purpose.** `GET /api/v1/health` stays open. It is where the
SPA learns that sign-in is required and which pool to sign in against
(`auth.region`, `auth.user_pool_id`, `auth.client_id`), and none of that is
secret — a pool id and a *public* app client id are visible to every browser
that signs in. It also still reports whether AWS credentials are present and
which providers are configured (booleans), as it always has.

**Still open at the infrastructure layer.** The Lambda Function URL is
`AuthType: NONE` and CloudFront has no viewer restriction. Anyone who learns
either URL can reach the *server*; they cannot get past the gate, but they
can make it verify tokens (a JWKS fetch on cold start, then in-memory) and,
in principle, run up invocation and egress charges. Neither door can be
closed for a browser that POSTs, which is the whole reason the gate lives in
the application:

**`AuthType: NONE` is not the same as reachable.** A Function URL with
`AuthType: NONE` is still closed until a resource policy opens it: every
request 403s with `Forbidden. For troubleshooting Function URL authorization
issues, see ...` until something grants `lambda:InvokeFunctionUrl` to
Principal `*`. SAM normally emits that permission for you — but only when
`AuthType` is the **literal** string `NONE`. Its test is
`auth_type not in ["NONE"]` (`samtranslator/model/sam_resources.py`,
`_construct_url_permission`), and once `AuthType` is `!Ref
ServerFunctionUrlAuthType` the value at transform time is an unresolved
intrinsic dict, which never equals `"NONE"`. Both permissions SAM would have
added are silently skipped. The stack still deploys clean — with a server
nothing can call.

Parameterising `AuthType` is what dropped them, and `sam validate --lint`
cannot catch it, because what is missing is a resource the transform declined
to add rather than a malformed one. Running the transform locally is what
shows it **[measured]**: the template as deployed produces *zero*
`AWS::Lambda::Permission` resources, while the same template with a literal
`NONE` produces `ServerFunctionUrlPublicPermissions`
(`lambda:InvokeFunctionUrl`) and `ServerFunctionURLInvokeAllowPublicAccess`
(`lambda:InvokeFunction` + `InvokedViaFunctionUrl`).

So the template declares both itself —
`ServerFunctionUrlPublicPermission` and `ServerFunctionUrlInvokePermission`,
under a `ServerFunctionUrlIsPublic` condition (`DeployServer` **and**
`AuthType == NONE`). That follows the parameter instead of the literal:
present while the URL is public, absent under `AWS_IAM`, where the caller's
SigV4 identity is the grant and a public policy would undo the point of
switching.

This is the bug `scripts/deploy_smoke.py` was written for, and it found it on
its first real run **[measured]**: `SPA is served at /` and `SPA deep link
resolves` both passed — CloudFront and S3 were fine — while `health responds`
retried ten times into that 403. Every other check in the pipeline was green.

**Why not `AWS_IAM` + CloudFront OAC.** CloudFront supports origin access
control for Lambda function URLs, and it would make the function reachable
only through the distribution. But with OAC, requests carrying a body require
the **viewer** to compute the SHA-256 of the request body and send it in
`x-amz-content-sha256` — Lambda does not accept unsigned payloads **[docs]**.
A browser cannot do that. Since this application POSTs to start every run and
every evaluation, OAC would break the app's primary path. It is viable only
for SigV4-signing non-browser clients; `ServerFunctionUrlAuthType` is
parameterized to `AWS_IAM` for that case and documented as not being a drop-in.

**The token path, hop by hop.** The browser holds the ID token in
`localStorage` (`evalharness.auth.v1`) and the SPA's HTTP layer adds the
bearer header to every JSON request and NDJSON stream **[repo]**. The `/api/*`
behaviour uses the managed `CachingDisabled` cache policy with the managed
`AllViewerExceptHostHeader` origin request policy. CloudFront ordinarily
forwards `Authorization` to a custom origin only when the *cache policy*
names it, with `CachingDisabled` documented as the exception under which an
origin request policy's headers — `Authorization` included — go through
**[docs]**. That is the combination this template already used before auth
existed, so nothing changed here; it is still listed under open risks
because "the header reached the function" is exactly the kind of thing only
a real request proves. The Function URL with `AuthType: NONE` passes the
`Authorization` header to the function untouched (it is consumed by Lambda
only under `AWS_IAM`, where it carries the SigV4 signature) **[docs]**.

**Why Cognito this way, and not the Hosted UI.** This is the
`readysetcloud/rsc-core` shape: the SPA calls `cognito-idp` directly with
`USER_PASSWORD_AUTH`, keeps `{idToken, refreshToken, expiresAt}` locally,
refreshes with `REFRESH_TOKEN_AUTH` and revokes on sign-out **[rsc]**.
No OAuth flows, no `UserPoolDomain`, no redirect round-trip, no SDK in the
bundle. Two deliberate deviations from rsc-core: the pool is
`AllowAdminCreateUserOnly` (an account here spends the AWS bill, so the
operator invites users with `make create-user`), and there is no
cross-subdomain cookie bridge (one origin).

**Verification, exactly.** RS256 against the pool's JWKS
(`https://cognito-idp.<region>.amazonaws.com/<pool>/.well-known/jwks.json`),
cached per process and re-fetched once when a token names an unknown `kid`
(rotation); `iss` must be the pool; `exp` is enforced; `token_use` must be
`id` (then `aud` = client id) or `access` (then `client_id` = client id).
These are the checks Cognito documents for verifying its JWTs **[docs]**.
An unreachable JWKS is a `502 upstream_error`, not a `401` — the server, not
the caller, is what is broken.

### IAM added, exactly

Beyond `AWSLambdaBasicExecutionRole` (logs) and the X-Ray write access SAM adds
for `Tracing: Active`:

| Actions | Resource |
|---|---|
| `bedrock:InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, `ConverseStream` | `foundation-model/*`, `inference-profile/*` |
| `bedrock:ListFoundationModels`, `ListInferenceProfiles` | `*` (neither is resource-scopable) |
| `bedrock:ApplyGuardrail`, `CreateGuardrail`, `CreateGuardrailVersion`, `GetGuardrail`, `UpdateGuardrail`, `DeleteGuardrail` | `guardrail/*` |
| `bedrock:ListGuardrails` | `*` |
| `dynamodb:Query`, `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` | `EvalTable` |
| `dynamodb:Query` | that table's `GSI1` |
| `lambda:InvokeFunction` | the eval worker function (only when it is deployed) |

Two differences from the worker's role are worth noting. The server **does**
get `Query` (listing runs and evaluations walks the `GSI1` `RUN`/`EVAL`
partitions) and `DeleteItem` (`DELETE /runs/{id}` is a real endpoint), where
the worker deliberately has neither. And guardrails here are full CRUD, not
just `ApplyGuardrail`.

---

## Deploying

```
make deploy            # = make deploy-backend && make deploy-frontend
```

is the whole flow, and it is **idempotent**. CI runs the same two targets as
separate jobs (`.github/workflows/pull-request.yaml` for stage,
`deploy.yaml` for prod), so there is one deploy definition.

`deploy-backend`:

1. Resolve the artifact bucket from the stack's `ArtifactBucket` output,
   bootstrapping the stack with a plain `sam deploy` if it does not exist yet.
2. `scripts/package-server.sh` → `server/<sha>.zip` and
   `scripts/package-eval-worker.sh` → `eval-worker/<sha>.zip`, both uploaded.
3. `sam build && sam deploy` with **both** `ServerArtifactKey` and
   `EvalWorkerArtifactKey` set.
4. Print `AppUrl` and the two env vars a local server needs for the cloud lane.

`deploy-frontend` (needs the backend to exist):

1. Resolve `AppBucket`, `AppDistributionId` and `AppUrl` from the stack.
2. `npm ci && VITE_API_URL=/ npm run build` in `app/`.
3. `aws s3 sync app/dist s3://<AppBucket> --delete`.
4. `aws cloudfront create-invalidation --paths '/*'`.
5. Print the `AppUrl`.

`make package-server` / `make package-eval-worker` build the artifacts alone
and make no AWS calls.

### The sharp edge

`EvalWorkerArtifactKey` and `ServerArtifactKey` are both plain CloudFormation
parameters with empty defaults, and an empty value deletes the corresponding
resources. `make deploy-backend` passes both every time, so it can never
delete either half; a bare `sam deploy` passes neither and deletes both. The
mitigation is the parameters' own comments in `infra/template.yaml`, the
Makefile comment, and this paragraph.

---

## Tearing it down

```
make destroy CONFIRM=llm-eval-harness
```

`delete-stack` alone does not do it, and the failure mode is expensive.
CloudFormation will not delete a bucket that still holds objects, and
`ArtifactBucket` sets `VersioningConfiguration: Status: Enabled`, so
`aws s3 rm --recursive` only writes delete markers — the bucket is still
non-empty as far as CloudFormation is concerned. The stack then sits in
`DELETE_FAILED` with `AppBucket` and `ArtifactBucket` orphaned, and the next
`make deploy-backend` fails too, because a stack in `DELETE_FAILED` accepts no
updates. **[measured]** on 2026-09-22, when exactly this blocked the staging
deploy on `claude/nice-ramanujan-x2wvoi`.

So `destroy` does it in the order that works:

1. Resolve `AppBucket` and `ArtifactBucket` from the stack's outputs (missing
   or already-gone outputs are skipped, not an error).
2. For each, `list-object-versions` and `delete-objects` in batches over both
   `Versions` and `DeleteMarkers`, looping until both come back empty.
3. `delete-stack`, then `wait stack-delete-complete`.

The `CONFIRM=` guard has to match `STACK_NAME` exactly; without it the target
prints what it would destroy and exits non-zero. Nothing else in the repo
deletes a stack, and CI never calls this target.

---

## What the first deploy proved

The stack deployed for the first time on 2026-09-17 from the `Staging`
environment (`Successfully created/updated stack - llm-eval-harness in
us-east-1`) **[measured]**. That converge, plus the resolved outputs,
settles the CloudFormation half of the list below:

- **The template converges.** Every resource created: table, worker runtime,
  server function, Function URL, artifact bucket, SPA bucket, OAC, CloudFront
  Function, distribution, Cognito pool and client.
- **`!Select [2, !Split ['/', !GetAtt ServerFunctionUrl.FunctionUrl]]`.** The
  distribution was created with that origin, so the implicit `<Function>Url`
  logical id and the string surgery both resolve to a real host.
- **OAC + `S3OriginConfig: {OriginAccessIdentity: ''}`** and **the
  bucket-policy / distribution ordering** are both accepted as written.
- **Auto-naming lengths hold.** `AppBucket` came back as
  `llm-eval-harness-appbucket-<11 chars>` (38 total), comfortably inside the
  63-char cap — though the longest stack name is still the one to compute
  against.
- **The post-CloudFormation steps run.** `aws s3 sync --delete` and
  `cloudfront create-invalidation` both succeeded under the deploy role, which
  is the exact point the predecessor repo's deploys used to fail.

**Still unproven, and deliberately so: everything that requires serving a
request.** Creating a Lambda function does not invoke it, so nothing below
under "Packaging and boot", "Streaming through CloudFront" or "Auth" is
settled by a successful deploy. The first `GET /api/v1/health` through
`AppUrl` is what proves the adapter boots at all.

## What the first green smoke settled

`scripts/deploy_smoke.py` first passed all five checks against a real stage
deploy on 2026-09-22 (run
[35763664350](https://github.com/allenheltondev/llm-eval-harness/actions/runs/35763664350))
**[measured]**:

```
PASS  health responds  (attempt 1)
PASS  auth is required and published  (pool us-east-1_ZDuoyZSbo)
PASS  protected route refuses anonymous callers
PASS  SPA is served at /
PASS  SPA deep link resolves
5 passed, 0 failed
```

That is anonymous HTTP through `AppUrl`, so it moves several items out of the
list below:

- **The adapter boots on arm64 and FastAPI answers.** `Handler: run.sh` +
  `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap` works for a *package* entrypoint on
  an arm64 managed runtime, and the artifact's manylinux wheels load. The
  first `/health` answered on attempt 1, cold start included, in about seven
  seconds.
- **`/api/*` reaches the application.** The protected route came back with the
  API's *own* 401 envelope rather than a CloudFront error or a 5xx — so the
  `/api/*` behaviour, the `AllViewerExceptHostHeader` origin request policy
  and the Function URL origin all work together.
- **`!Select [2, !Split ['/', !GetAtt ServerFunctionUrl.FunctionUrl]]`
  produces a working origin domain.** The string surgery is right against a
  real `FunctionUrl`.
- **OAC + `S3OriginConfig: {OriginAccessIdentity: ''}` and the SPA fallback.**
  `/` and a deep link both return HTML, so the empty-string-alongside-OAC form
  is correct and the custom error responses route to `index.html`.
- **Auth is switched on in a deployed stack** and the pool the SPA signs in
  against is published on `/health`.

What it still does not touch, because it carries no token: `Authorization`
surviving CloudFront, NDJSON staying incremental, the SPA's call to
`cognito-idp`, and everything about the eval worker.

This run is also where the missing Function URL resource policy was caught —
see "`AuthType: NONE` is not the same as reachable" above.

## Open risks — still unverified

Everything below is reasoned or read, not observed. Some of it was settled by
the smoke run described above; where the two disagree, the section above is
the measurement.

**Packaging and boot**

- **`Handler: run.sh` + `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap`.** Copied from
  an AWS-maintained example **[lwa]**, but that example is x86 and does not use
  a package (`evalharness.main:app` vs `main:app`). The import of a *package*
  from `$LAMBDA_TASK_ROOT` rather than a top-level module is the one untested
  step.
- **The `python3` substitution.** Near-certain, but it is a deviation from the
  working example.
- **`manylinux_2_28` floor.** Inherited from the worker's build, where greenlet
  forced it. Lambda's `python3.12` runtime is Amazon Linux 2023 (glibc 2.34), so
  it should hold; `SERVER_PYTHON_PLATFORM` is the escape hatch.
- **Cold start, unmeasured.** Importing strands + boto3 + FastAPI + SQLModel
  before uvicorn can answer the readiness probe is not fast, and the adapter
  waits for it. `ServerMemorySize` (default 1024) is the knob; consider 2048 if
  the first request after a deploy is painful. `AWS_LWA_ASYNC_INIT=true` is the
  adapter's own answer to this **[lwa]** and is not set — it changes init
  semantics and should be tried against a real cold start, not guessed at.

**Streaming through CloudFront**

- **That NDJSON actually stays incremental end to end.** Each hop is documented
  to stream (uvicorn → adapter with `response_stream` → Function URL with
  `RESPONSE_STREAM` → CloudFront), but "no hop buffers the whole body" is
  precisely what only a real run proves. If it turns out CloudFront buffers,
  the fallback is pointing the SPA at the Function URL directly and accepting
  CORS.
- **The 60 s inter-packet timeout.** Whether real runs ever go a full minute
  between events is unknown.

**Auth**

- **`Authorization` reaches the function through CloudFront.** Reasoned from
  the `CachingDisabled` + `AllViewerExceptHostHeader` pairing above
  **[docs]**; if a deployed `GET /api/v1/runs` through `AppUrl` answers `401`
  with a token that works against `ServerFunctionUrl` directly, this is the
  first thing to check, and the fix is a custom cache policy that lists
  `Authorization` rather than anything in the application.
- **The Function URL forwards `Authorization` under `AuthType: NONE`.** Read,
  not observed; same symptom, checked with `curl -H "Authorization: Bearer
  ..."` against `ServerFunctionUrl`.
- **The JWKS fetch on cold start.** `evalharness.auth` fetches the pool's keys
  with a 5 s timeout from inside the Lambda over the public internet — there
  is no VPC, so this is a plain outbound HTTPS call, but it is one more thing
  the first authenticated request after a cold start pays for.
- **Cognito `admin-create-user` email delivery.** The pool uses Cognito's
  default email sender (50 messages/day/account, no SES). Enough for
  inviting a handful of users; not enough for anything else, and the reason
  `make create-user` says "a temporary password is on its way" rather than
  printing one.
- **The SPA's direct call to `cognito-idp.<region>.amazonaws.com`.** Cognito's
  user-pool API is CORS-enabled for browsers by design (it is how rsc-core's
  consumers work today **[rsc]**), but this deployment has not made that
  call from a CloudFront-served origin yet.

**CloudFront and CFN mechanics**

- **`!Select [2, !Split ['/', !GetAtt ServerFunctionUrl.FunctionUrl]]`.** The
  implicit `<Function>Url` logical id is how AWS's own example reads the URL
  back **[lwa]**; the string surgery is straightforward but unverified against
  a real `FunctionUrl` value.
- **OAC + `S3OriginConfig: {OriginAccessIdentity: ''}`.** The documented
  pairing, but the empty-string-alongside-OAC form is easy to get subtly wrong.
- **The bucket-policy / distribution ordering.** `AppBucketPolicy` references
  the distribution and the distribution references the bucket's domain name;
  CloudFormation should order this fine (the policy is not an input to the
  distribution), but a first-create failure here would be unsurprising.
- **A second deploy over an existing stack.** The first create is proven; an
  *update* (new artifact keys against live resources, CloudFront distribution
  in place) is a different code path and has not run yet.
