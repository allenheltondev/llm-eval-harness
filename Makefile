.PHONY: dev dev-server dev-app lint lint-app lint-server test test-app test-server \
	install install-app install-server validate-template e2e smoke \
	package-eval-worker package-server deploy-backend deploy-frontend deploy create-user grant-access revoke-access \
	destroy

# CloudFormation stack the infra/ SAM template deploys into. Overriding this is
# what makes multiple environments possible (Staging and Production are two
# independent stacks), so it MUST reach `sam deploy` itself -- samconfig.toml
# carries its own stack_name and would otherwise win, silently deploying every
# environment into the same stack. See SAM_DEPLOY_ARGS below.
STACK_NAME ?= llm-eval-harness
# Region for deploys. Empty means "whatever samconfig.toml/AWS_REGION says";
# CI sets it explicitly so the stack can never land in a surprise region.
DEPLOY_REGION ?=
# Threaded into every `sam deploy`. Do not inline these flags at the call
# sites -- they must stay identical. `--resolve-s3` is SAM's own managed
# bucket for the packaged template; the server/worker zips go to the bucket
# the stack itself creates (ArtifactBucket).
SAM_DEPLOY_ARGS ?= --stack-name $(STACK_NAME) \
	$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
	--resolve-s3 \
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
	(cd server && uv run uvicorn nimbus.main:app --reload --port 8000) & \
	(cd app && npm run dev) & \
	wait

# Fake-model mode (zero AWS calls, scripted model + judge):
#   NIMBUS_FAKE_MODEL=1 make dev

dev-server:
	cd server && uv run uvicorn nimbus.main:app --reload --port 8000

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
# deploy
#
# Two halves, one definition, shared by local runs and CI (.github/workflows):
#
#   deploy-backend   package the server + eval worker zips -> upload to the
#                    stack's ArtifactBucket -> sam deploy the whole stack
#   deploy-frontend  build the SPA against the deployed stack -> s3 sync ->
#                    CloudFront invalidation
#   deploy           both, in that order
#
# `package-server` / `package-eval-worker` build the zips alone and make no
# AWS calls. Both artifact keys are content-hashed and BOTH are always passed
# to `sam deploy`, so there is no way for one half to delete the other (the
# template's artifact-key parameters default to '', and '' deletes the
# resource -- see their comments in infra/template.yaml). Never run a bare
# `sam deploy` against a deployed stack for that reason.
#
# Optional overrides:
#   SERVER_MEMORY=2048 make deploy-backend      # bigger Lambda (faster cold start)
#   HISTORY_RETENTION_DAYS=365 make deploy-backend  # expire history after a year (default: keep forever)
#   make deploy-backend ACCESS_GROUP_NAME=nimbus-team  # access group on the shared pool (default: the stack name)
#   DEPLOY_API_URL=https://... make deploy-frontend   # SPA pointed elsewhere
# --------------------------------------------------------------------------- #

package-eval-worker:
	EVAL_WORKER_BUILD_DIR=$(EVAL_WORKER_BUILD_DIR) ./scripts/package-eval-worker.sh

package-server:
	SERVER_BUILD_DIR=$(SERVER_BUILD_DIR) ./scripts/package-server.sh

deploy-backend:
	@set -e; \
	DEPLOY_REGION=$(DEPLOY_REGION) ./scripts/check-deploy-prerequisites.sh $(STACK_NAME); \
	resolve_output() { \
		aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
			$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
			--query "Stacks[0].Outputs[?OutputKey=='$$1'].OutputValue" \
			--output text 2>/dev/null || true; \
	}; \
	BUCKET=$$(resolve_output ArtifactBucket); \
	if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "None" ]; then \
		echo "deploy-backend: stack '$(STACK_NAME)' has no artifact bucket yet -- bootstrapping"; \
		( cd infra && sam build && sam deploy $(SAM_DEPLOY_ARGS) ); \
		BUCKET=$$(resolve_output ArtifactBucket); \
	fi; \
	if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "None" ]; then \
		echo "deploy-backend: could not resolve ArtifactBucket from stack '$(STACK_NAME)'" >&2; \
		exit 1; \
	fi; \
	SERVER_BUILD_DIR=$(SERVER_BUILD_DIR) ./scripts/package-server.sh; \
	. $(SERVER_BUILD_DIR)/artifact.env; SERVER_KEY="$$ARTIFACT_KEY"; SERVER_ZIP="$$ARTIFACT_ZIP"; \
	EVAL_WORKER_BUILD_DIR=$(EVAL_WORKER_BUILD_DIR) ./scripts/package-eval-worker.sh; \
	. $(EVAL_WORKER_BUILD_DIR)/artifact.env; WORKER_KEY="$$ARTIFACT_KEY"; WORKER_ZIP="$$ARTIFACT_ZIP"; \
	echo "deploy-backend: uploading $$SERVER_KEY and $$WORKER_KEY to s3://$$BUCKET"; \
	aws s3 cp "$$SERVER_ZIP" "s3://$$BUCKET/$$SERVER_KEY" $(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),); \
	aws s3 cp "$$WORKER_ZIP" "s3://$$BUCKET/$$WORKER_KEY" $(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),); \
	( cd infra && sam build && sam deploy $(SAM_DEPLOY_ARGS) --parameter-overrides \
		"ServerArtifactKey=$$SERVER_KEY" \
		"EvalWorkerArtifactKey=$$WORKER_KEY" \
		$${SERVER_MEMORY:+"ServerMemorySize=$$SERVER_MEMORY"} \
		$${HISTORY_RETENTION_DAYS:+"HistoryRetentionDays=$$HISTORY_RETENTION_DAYS"} \
		$(if $(ACCESS_GROUP_NAME),"AccessGroupName=$(ACCESS_GROUP_NAME)",) ); \
	echo; \
	echo "Backend deployed to stack $(STACK_NAME):"; \
	echo "  AppUrl:                        $$(resolve_output AppUrl)"; \
	echo "  NIMBUS_EVAL_FUNCTION_NAME=$$(resolve_output EvalWorkerFunctionName)"; \
	echo "  NIMBUS_EVAL_TABLE=$$(resolve_output TableName)"

deploy-frontend:
	@set -e; \
	resolve_output() { \
		aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
			$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
			--query "Stacks[0].Outputs[?OutputKey=='$$1'].OutputValue" \
			--output text 2>/dev/null || true; \
	}; \
	APP_BUCKET=$$(resolve_output AppBucket); \
	DIST_ID=$$(resolve_output AppDistributionId); \
	APP_URL=$$(resolve_output AppUrl); \
	if [ -z "$$APP_BUCKET" ] || [ "$$APP_BUCKET" = "None" ]; then \
		echo "deploy-frontend: stack '$(STACK_NAME)' has no AppBucket output -- run make deploy-backend first" >&2; \
		exit 1; \
	fi; \
	echo "deploy-frontend: building the SPA with VITE_API_URL=$(DEPLOY_API_URL)"; \
	( cd app && npm ci && VITE_API_URL="$(DEPLOY_API_URL)" npm run build ); \
	aws s3 sync app/dist "s3://$$APP_BUCKET" --delete $(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),); \
	echo "deploy-frontend: invalidating $$DIST_ID"; \
	aws cloudfront create-invalidation --distribution-id "$$DIST_ID" --paths '/*' >/dev/null; \
	echo; \
	echo "Deployed: $$APP_URL"

deploy: deploy-backend deploy-frontend

# --------------------------------------------------------------------------- #
# users
#
# The deployed app signs in against the Ready, Set, Cloud shared pool
# (infra/template.yaml `AuthUserPoolId`), where anyone can create an account.
# An account is not access: the server also requires the stack's group
# (`AccessGroupName` output). These add and remove people from it.
#   make grant-access EMAIL=you@example.com [STACK_NAME=llm-eval-harness-staging]
#   make revoke-access EMAIL=you@example.com
#   make create-user EMAIL=you@example.com   # no RSC account yet: invite, then grant
# --------------------------------------------------------------------------- #

# Resolves POOL_ID and GROUP from the stack's outputs, or stops with why.
define resolve_access
	resolve_output() { \
		aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
			$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
			--query "Stacks[0].Outputs[?OutputKey=='$$1'].OutputValue" \
			--output text 2>/dev/null || true; \
	}; \
	if [ -z "$(EMAIL)" ]; then \
		echo "$@: EMAIL is required, e.g. make $@ EMAIL=you@example.com" >&2; \
		exit 1; \
	fi; \
	POOL_ID=$$(resolve_output UserPoolId); \
	GROUP=$$(resolve_output AccessGroupName); \
	if [ -z "$$GROUP" ] || [ "$$GROUP" = "None" ]; then \
		echo "$@: stack '$(STACK_NAME)' has no AccessGroupName output -- is the server deployed (make deploy)?" >&2; \
		exit 1; \
	fi
endef

grant-access:
	@set -e; \
	$(resolve_access); \
	if ! OUT=$$(aws cognito-idp admin-add-user-to-group \
		$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
		--user-pool-id "$$POOL_ID" --username "$(EMAIL)" --group-name "$$GROUP" 2>&1); then \
		case "$$OUT" in \
			*UserNotFoundException*) \
				echo "grant-access: $(EMAIL) has no Ready, Set, Cloud account yet -- they can create one on the sign-in screen, or run make create-user EMAIL=$(EMAIL)" >&2 ;; \
			*) echo "grant-access: $$OUT" >&2 ;; \
		esac; \
		exit 1; \
	fi; \
	echo "grant-access: $(EMAIL) may now use $(STACK_NAME) (group $$GROUP in pool $$POOL_ID); if they are signed in already, they sign out and back in"

revoke-access:
	@set -e; \
	$(resolve_access); \
	aws cognito-idp admin-remove-user-from-group \
		$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
		--user-pool-id "$$POOL_ID" --username "$(EMAIL)" --group-name "$$GROUP"; \
	echo "revoke-access: $(EMAIL) removed from $$GROUP; tokens already issued keep working until they expire (up to an hour)"

# For someone with no Ready, Set, Cloud account: Cognito emails them a
# temporary password (the sign-in screen walks them through choosing a real
# one), and they are granted this stack in the same step.
create-user:
	@set -e; \
	$(resolve_access); \
	aws cognito-idp admin-create-user \
		$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
		--user-pool-id "$$POOL_ID" \
		--username "$(EMAIL)" \
		--user-attributes Name=email,Value="$(EMAIL)" Name=email_verified,Value=true \
		--desired-delivery-mediums EMAIL >/dev/null; \
	aws cognito-idp admin-add-user-to-group \
		$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),) \
		--user-pool-id "$$POOL_ID" --username "$(EMAIL)" --group-name "$$GROUP"; \
	echo "create-user: invited $(EMAIL) to pool $$POOL_ID and granted $(STACK_NAME) -- a temporary password is on its way by email"

# Tear the stack down. Guarded behind CONFIRM= because it deletes the history
# table, the user pool and both buckets -- everything the deployment has.
#
# This exists because `aws cloudformation delete-stack` on its own does NOT
# work here, and finding that out costs an hour: CloudFormation refuses to
# delete a non-empty bucket, and ArtifactBucket has versioning enabled, so
# `aws s3 rm --recursive` does not empty it either -- every object version and
# delete marker has to go. A stack that fails this way lands in DELETE_FAILED
# with the buckets orphaned, which then blocks the next deploy too.
destroy:
	@set -e; \
	if [ "$(CONFIRM)" != "$(STACK_NAME)" ]; then \
		echo "destroy: this DELETES the stack '$(STACK_NAME)' and everything in it"; \
		echo "         (history table, user pool, SPA bucket, artifact bucket)."; \
		echo; \
		echo "  make destroy CONFIRM=$(STACK_NAME)"; \
		exit 1; \
	fi; \
	REGION_ARG="$(if $(DEPLOY_REGION),--region $(DEPLOY_REGION),)"; \
	resolve_output() { \
		aws cloudformation describe-stacks --stack-name $(STACK_NAME) $$REGION_ARG \
			--query "Stacks[0].Outputs[?OutputKey=='$$1'].OutputValue" \
			--output text 2>/dev/null || true; \
	}; \
	empty_bucket() { \
		bucket="$$1"; \
		if [ -z "$$bucket" ] || [ "$$bucket" = "None" ]; then return 0; fi; \
		echo "==> emptying s3://$$bucket (including every version)"; \
		aws s3 rm "s3://$$bucket" --recursive $$REGION_ARG >/dev/null 2>&1 || true; \
		while :; do \
			versions=$$(aws s3api list-object-versions --bucket "$$bucket" $$REGION_ARG \
				--max-items 500 \
				--query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
				--output json 2>/dev/null || echo '{"Objects":null}'); \
			markers=$$(aws s3api list-object-versions --bucket "$$bucket" $$REGION_ARG \
				--max-items 500 \
				--query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
				--output json 2>/dev/null || echo '{"Objects":null}'); \
			done_versions=1; \
			case "$$versions" in *'"Objects": null'*|*'"Objects":null'*) ;; *) \
				aws s3api delete-objects --bucket "$$bucket" $$REGION_ARG \
					--delete "$$versions" >/dev/null; done_versions=0 ;; \
			esac; \
			case "$$markers" in *'"Objects": null'*|*'"Objects":null'*) ;; *) \
				aws s3api delete-objects --bucket "$$bucket" $$REGION_ARG \
					--delete "$$markers" >/dev/null; done_versions=0 ;; \
			esac; \
			if [ "$$done_versions" = "1" ]; then break; fi; \
		done; \
	}; \
	empty_bucket "$$(resolve_output AppBucket)"; \
	empty_bucket "$$(resolve_output ArtifactBucket)"; \
	echo "==> deleting stack $(STACK_NAME)"; \
	aws cloudformation delete-stack --stack-name $(STACK_NAME) $$REGION_ARG; \
	aws cloudformation wait stack-delete-complete --stack-name $(STACK_NAME) $$REGION_ARG; \
	echo "==> $(STACK_NAME) deleted"
