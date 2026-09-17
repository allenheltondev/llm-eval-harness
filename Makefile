.PHONY: dev dev-server dev-app lint lint-app lint-server test test-app test-server \
	install install-app install-server validate-template e2e smoke \
	package-eval-worker deploy-worker package-server deploy create-user

# CloudFormation stack the infra/ SAM template deploys into. Overriding this is
# what makes multiple environments possible (Staging and Production are two
# independent stacks), so it MUST reach `sam deploy` itself -- samconfig.toml
# carries its own stack_name and would otherwise win, silently deploying every
# environment into the same stack. See SAM_DEPLOY_ARGS below.
STACK_NAME ?= llm-eval-harness
# Region for deploys. Empty means "whatever samconfig.toml/AWS_REGION says";
# CI sets it explicitly so the stack can never land in a surprise region.
DEPLOY_REGION ?=
# Shared org artifacts bucket for SAM's packaging (CI passes
# secrets.ARTIFACTS_BUCKET_NAME). Empty locally -> --resolve-s3 and SAM's own
# managed bucket. The two are mutually exclusive, hence the either/or below.
DEPLOY_S3_BUCKET ?=
# Role CloudFormation itself assumes to create resources (CI passes
# secrets.CLOUDFORMATION_EXECUTION_ROLE). Empty locally -> your own creds.
DEPLOY_ROLE_ARN ?=
# Threaded into every `sam deploy`. Do not inline these flags at the call
# sites -- there are five of them and they must stay identical.
SAM_DEPLOY_ARGS ?= --stack-name $(STACK_NAME) \
	$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
	$(if $(DEPLOY_S3_BUCKET),--s3-bucket $(DEPLOY_S3_BUCKET),--resolve-s3) \
	$(if $(DEPLOY_ROLE_ARN),--role-arn $(DEPLOY_ROLE_ARN),) \
	--no-fail-on-empty-changeset
# Where scripts/package-eval-worker.sh stages and zips the worker artifact.
EVAL_WORKER_BUILD_DIR ?= $(CURDIR)/.build/eval-worker
# Where scripts/package-server.sh stages and zips the FastAPI server artifact.
SERVER_BUILD_DIR ?= $(CURDIR)/.build/server
# Baked into the SPA at build time. "/" (not "") is deliberate: app/src/api/http.ts
# treats a blank VITE_API_URL as unset and falls back to http://localhost:8000,
# whereas "/" trims to "" and makes apiUrl() emit relative "/api/v1/..." paths --
# which is exactly right behind CloudFront, where the SPA and the API share an
# origin. Override to point a build at a server somewhere else.
DEPLOY_API_URL ?= /

# --------------------------------------------------------------------------- #
# dev
#
# Runs the API server (uvicorn --reload, :8000) and the web app (vite, :3000)
# concurrently in one terminal. The trap forwards Ctrl-C (and any exit) to
# both background jobs so neither is left running after you stop `make dev`.
#
# Recipe intentionally does NOT call `$(MAKE) dev-server`/`$(MAKE) dev-app`:
# GNU Make always executes recipe lines that reference $(MAKE), even under
# `make -n` (dry-run), which would defeat dry-run entirely. Run the two
# pieces directly instead; use `make dev-server` / `make dev-app` if you'd
# rather run them in two separate terminals (e.g. to keep their logs apart).
dev:
	@trap 'kill 0' EXIT INT TERM; \
	(cd server && uv run uvicorn evalharness.main:app --reload --port 8000) & \
	(cd app && npm run dev) & \
	wait

# Fake-model mode (zero AWS calls, scripted model + judge):
#   EVALHARNESS_FAKE_MODEL=1 make dev

dev-server:
	cd server && uv run uvicorn evalharness.main:app --reload --port 8000

dev-app:
	cd app && npm run dev

# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

install: install-app install-server

install-app:
	cd app && npm ci

install-server:
	cd server && uv sync --dev

# --------------------------------------------------------------------------- #
# lint
# --------------------------------------------------------------------------- #

lint: lint-app lint-server

lint-app:
	cd app && npm run lint

lint-server:
	cd server && uv run ruff check .

# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #

test: test-app test-server

test-app:
	cd app && npm test

# cfn-lint via sam: no AWS credentials needed. CI runs the same target.
validate-template:
	cd infra && sam validate --lint --region $(or $(DEPLOY_REGION),us-east-1)

test-server:
	cd server && uv run pytest

# --------------------------------------------------------------------------- #
# e2e / smoke
#
# `e2e` runs the Playwright suite against the fake-model full stack (server +
# app), which `playwright.config.ts` boots itself as `webServer`s -- no AWS
# credentials needed, nothing to start by hand.
#
# `smoke` runs the gated, real-AWS smoke script (scripts/live_smoke.py)
# against an already-running, non-fake-model server. It refuses to do
# anything (and makes no network calls) unless RUN_LIVE_BEDROCK=1 is set, so
# this target never fires real Bedrock calls on its own -- e.g.:
#   RUN_LIVE_BEDROCK=1 make smoke
# --------------------------------------------------------------------------- #

e2e:
	cd app && npx playwright test

smoke:
	@if [ "$$RUN_LIVE_BEDROCK" != "1" ]; then \
		echo "smoke: refusing to run -- this hits real, billed AWS Bedrock calls." >&2; \
		echo "       set RUN_LIVE_BEDROCK=1 explicitly to proceed, e.g.:" >&2; \
		echo "         RUN_LIVE_BEDROCK=1 make smoke" >&2; \
		exit 1; \
	fi
	cd server && uv run python ../scripts/live_smoke.py

# --------------------------------------------------------------------------- #
# cloud eval worker
#
# `package-eval-worker` builds the AgentCore CodeZip artifact and nothing else
# -- no AWS calls, safe to run anywhere. `deploy-worker` builds it, uploads it,
# and deploys the whole infra/ stack with the worker enabled.
#
# Once the worker exists, keep deploying through this target or `make deploy`:
# a bare `sam deploy` passes no EvalWorkerArtifactKey, so the parameter falls
# back to its empty default and CloudFormation deletes the runtime. (See the
# parameter's own comment in infra/template.yaml.)
# --------------------------------------------------------------------------- #

package-eval-worker:
	EVAL_WORKER_BUILD_DIR=$(EVAL_WORKER_BUILD_DIR) ./scripts/package-eval-worker.sh

deploy-worker:
	@set -e; \
	resolve_output() { \
		aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='$$1'].OutputValue" \
			--output text 2>/dev/null || true; \
	}; \
	if [ -n "$(DEPLOY_S3_BUCKET)" ]; then \
		BUCKET="$(DEPLOY_S3_BUCKET)"; \
		echo "deploy-worker: using the supplied artifact bucket $$BUCKET"; \
	else \
		BUCKET=$$(resolve_output EvalWorkerArtifactBucket); \
		if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "None" ]; then \
			echo "deploy-worker: stack '$(STACK_NAME)' has no artifact bucket yet -- bootstrapping"; \
			( cd infra && sam build && sam deploy $(SAM_DEPLOY_ARGS) ); \
			BUCKET=$$(resolve_output EvalWorkerArtifactBucket); \
		fi; \
	fi; \
	if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "None" ]; then \
		echo "deploy-worker: could not resolve EvalWorkerArtifactBucket from stack '$(STACK_NAME)'" >&2; \
		exit 1; \
	fi; \
	EVAL_WORKER_BUILD_DIR=$(EVAL_WORKER_BUILD_DIR) ./scripts/package-eval-worker.sh; \
	. $(EVAL_WORKER_BUILD_DIR)/artifact.env; \
	echo "deploy-worker: uploading $$ARTIFACT_KEY to s3://$$BUCKET"; \
	aws s3 cp "$$ARTIFACT_ZIP" "s3://$$BUCKET/$$ARTIFACT_KEY"; \
	CURRENT_SERVER_KEY=$$(aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
		--query "Stacks[0].Parameters[?ParameterKey=='ServerArtifactKey'].ParameterValue" \
		--output text 2>/dev/null || true); \
	if [ "$$CURRENT_SERVER_KEY" = "None" ]; then CURRENT_SERVER_KEY=""; fi; \
	if [ -n "$$CURRENT_SERVER_KEY" ]; then \
		echo "deploy-worker: preserving deployed server artifact $$CURRENT_SERVER_KEY"; \
	fi; \
	( cd infra && sam build && sam deploy $(SAM_DEPLOY_ARGS) --parameter-overrides \
		"EvalWorkerArtifactKey=$$ARTIFACT_KEY" \
		$${DEPLOY_S3_BUCKET:+"ArtifactsBucketName=$(DEPLOY_S3_BUCKET)"} \
		$${CURRENT_SERVER_KEY:+"ServerArtifactKey=$$CURRENT_SERVER_KEY"} ); \
	ARN=$$(resolve_output EvalWorkerRuntimeArn); \
	TABLE=$$(resolve_output TableName); \
	echo; \
	echo "Cloud eval lane deployed. Point the server at it:"; \
	echo "  EVALHARNESS_EVAL_RUNTIME_ARN=$$ARN"; \
	echo "  EVALHARNESS_EVAL_TABLE=$$TABLE"

# --------------------------------------------------------------------------- #
# deployed server + SPA (docs/serverless-deploy-infra.md)
#
# `package-server` builds the server's Lambda zip and nothing else -- no AWS
# calls, safe to run anywhere. `deploy` is the whole thing:
#
#   package -> upload -> sam deploy -> build SPA -> s3 sync -> invalidate
#
# `deploy` is orthogonal to deploy-worker: it passes ServerArtifactKey, and
# reads the stack's CURRENT EvalWorkerArtifactKey and passes that back
# unchanged, so deploying the server never deletes a deployed eval worker. The converse is NOT true --
# `deploy-worker` does not preserve ServerArtifactKey, so once the server
# exists, `make deploy` is the target to use.
#
# Optional overrides:
#   SERVER_MEMORY=2048 make deploy       # bigger Lambda (faster cold start)
#   DEPLOY_API_URL=https://... make deploy   # SPA pointed elsewhere
# --------------------------------------------------------------------------- #

package-server:
	SERVER_BUILD_DIR=$(SERVER_BUILD_DIR) ./scripts/package-server.sh

deploy:
	@set -e; \
	resolve_output() { \
		aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='$$1'].OutputValue" \
			--output text 2>/dev/null || true; \
	}; \
	if [ -n "$(DEPLOY_S3_BUCKET)" ]; then \
		BUCKET="$(DEPLOY_S3_BUCKET)"; \
		echo "deploy: using the supplied artifact bucket $$BUCKET"; \
	else \
		BUCKET=$$(resolve_output ArtifactBucket); \
		if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "None" ]; then \
			echo "deploy: stack '$(STACK_NAME)' has no artifact bucket yet -- bootstrapping"; \
			( cd infra && sam build && sam deploy $(SAM_DEPLOY_ARGS) ); \
			BUCKET=$$(resolve_output ArtifactBucket); \
		fi; \
	fi; \
	if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "None" ]; then \
		echo "deploy: could not resolve ArtifactBucket from stack '$(STACK_NAME)'" >&2; \
		exit 1; \
	fi; \
	SERVER_BUILD_DIR=$(SERVER_BUILD_DIR) ./scripts/package-server.sh; \
	. $(SERVER_BUILD_DIR)/artifact.env; \
	echo "deploy: uploading $$ARTIFACT_KEY to s3://$$BUCKET"; \
	aws s3 cp "$$ARTIFACT_ZIP" "s3://$$BUCKET/$$ARTIFACT_KEY"; \
	WORKER_KEY=$$(aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
		--query "Stacks[0].Parameters[?ParameterKey=='EvalWorkerArtifactKey'].ParameterValue" \
		--output text 2>/dev/null || true); \
	if [ -n "$$WORKER_KEY" ] && [ "$$WORKER_KEY" != "None" ]; then \
		echo "deploy: preserving deployed eval worker artifact $$WORKER_KEY"; \
	else \
		WORKER_KEY=""; \
	fi; \
	( cd infra && sam build && sam deploy $(SAM_DEPLOY_ARGS) --parameter-overrides \
		"ServerArtifactKey=$$ARTIFACT_KEY" \
		$${DEPLOY_S3_BUCKET:+"ArtifactsBucketName=$(DEPLOY_S3_BUCKET)"} \
		$${WORKER_KEY:+"EvalWorkerArtifactKey=$$WORKER_KEY"} \
		$${SERVER_MEMORY:+"ServerMemorySize=$$SERVER_MEMORY"} ); \
	APP_BUCKET=$$(resolve_output AppBucket); \
	DIST_ID=$$(resolve_output AppDistributionId); \
	APP_URL=$$(resolve_output AppUrl); \
	if [ -z "$$APP_BUCKET" ] || [ "$$APP_BUCKET" = "None" ]; then \
		echo "deploy: could not resolve AppBucket from stack '$(STACK_NAME)'" >&2; \
		exit 1; \
	fi; \
	echo "deploy: building the SPA with VITE_API_URL=$(DEPLOY_API_URL)"; \
	( cd app && npm ci && VITE_API_URL="$(DEPLOY_API_URL)" npm run build ); \
	aws s3 sync app/dist "s3://$$APP_BUCKET" --delete; \
	echo "deploy: invalidating $$DIST_ID"; \
	aws cloudfront create-invalidation --distribution-id "$$DIST_ID" --paths '/*' >/dev/null; \
	echo; \
	echo "Deployed: $$APP_URL"

# --------------------------------------------------------------------------- #
# users
#
# The deployed app's Cognito pool is invitation-only (infra/template.yaml
# `UserPool`). This invites one user: Cognito emails them a temporary password
# and the app's sign-in screen walks them through choosing a real one.
#   make create-user EMAIL=you@example.com [STACK_NAME=llm-eval-harness-staging]
# --------------------------------------------------------------------------- #

create-user:
	@set -e; \
	if [ -z "$(EMAIL)" ]; then \
		echo "create-user: EMAIL is required, e.g. make create-user EMAIL=you@example.com" >&2; \
		exit 1; \
	fi; \
	POOL_ID=$$(aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
		$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
		--query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
		--output text 2>/dev/null || true); \
	if [ -z "$$POOL_ID" ] || [ "$$POOL_ID" = "None" ]; then \
		echo "create-user: stack '$(STACK_NAME)' has no UserPoolId output -- is the server deployed (make deploy)?" >&2; \
		exit 1; \
	fi; \
	aws cognito-idp admin-create-user \
		$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
		--user-pool-id "$$POOL_ID" \
		--username "$(EMAIL)" \
		--user-attributes Name=email,Value="$(EMAIL)" Name=email_verified,Value=true \
		--desired-delivery-mediums EMAIL >/dev/null; \
	echo "create-user: invited $(EMAIL) to pool $$POOL_ID -- a temporary password is on its way by email"
