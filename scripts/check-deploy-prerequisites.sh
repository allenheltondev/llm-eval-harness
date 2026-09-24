#!/usr/bin/env bash
#
# Refuses a backend deploy the running stack is not ready for.
#
# CloudFormation applies a Lambda's configuration and its code in separate
# calls, and requests are served in between, so some releases are only safe
# on top of an earlier one having *succeeded* -- not merely having run first.
# The production workflow's concurrency group queues deploys in order, but a
# release after a failed deploy still runs. This check is what gates it.
#
# Current prerequisites:
#
# * the eval worker must already be running with Handler
#   nimbus.worker.lambda_app.handler. This release's worker zip no longer
#   carries the pre-rename `evalharness` shim, so deploying it onto a worker
#   still configured with the old handler would leave the function unable to
#   import its handler until the configuration update lands.
# * the live server must already enforce NIMBUS_AUTH_REQUIRED_GROUP (its
#   /health says `supports_required_group: true`). This release points the
#   server at the shared Ready, Set, Cloud pool, where anyone can sign up; if
#   that configuration landed on code that ignores the group, every account in
#   the pool could use the stack until the new code did.
#
# Fails closed: anything it cannot read (a permission, a throttle) stops the
# deploy with the reason, rather than being taken as "fine". A stack or worker
# that does not exist yet has nothing to be incompatible with, and passes.
#
# Usage: scripts/check-deploy-prerequisites.sh STACK_NAME   (honours DEPLOY_REGION)

set -euo pipefail

STACK="${1:?usage: check-deploy-prerequisites.sh STACK_NAME}"
REQUIRED_HANDLER="nimbus.worker.lambda_app.handler"
API_PREFIX="/api/v1"
REGION_ARGS=()
if [ -n "${DEPLOY_REGION:-}" ]; then
  REGION_ARGS=(--region "${DEPLOY_REGION}")
fi

fail() {
  echo "check-deploy-prerequisites: $*" >&2
  exit 1
}

check_worker_handler() {
  local resource_out handler
  if ! resource_out="$(aws cloudformation describe-stack-resource \
        --stack-name "${STACK}" \
        --logical-resource-id EvalWorkerFunction \
        --query StackResourceDetail.PhysicalResourceId \
        --output text ${REGION_ARGS[@]+"${REGION_ARGS[@]}"} 2>&1)"; then
    case "${resource_out}" in
      *"does not exist"*)
        echo "check-deploy-prerequisites: no eval worker on '${STACK}' yet; nothing to check"
        return 0
        ;;
      *)
        fail "could not look up the eval worker on '${STACK}': ${resource_out}"
        ;;
    esac
  fi

  if ! handler="$(aws lambda get-function-configuration \
        --function-name "${resource_out}" \
        --query Handler \
        --output text ${REGION_ARGS[@]+"${REGION_ARGS[@]}"} 2>&1)"; then
    fail "could not read the eval worker's handler (${resource_out}): ${handler}"
  fi

  if [ "${handler}" != "${REQUIRED_HANDLER}" ]; then
    fail "the eval worker on '${STACK}' still runs Handler '${handler}'. This release needs ${REQUIRED_HANDLER} to be live first: deploy the release that switches it (#7) successfully, then retry."
  fi

  echo "check-deploy-prerequisites: eval worker on '${STACK}' runs ${REQUIRED_HANDLER}; ok"
}

check_server_enforces_group() {
  local app_url health supports
  if ! app_url="$(aws cloudformation describe-stacks \
        --stack-name "${STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" \
        --output text ${REGION_ARGS[@]+"${REGION_ARGS[@]}"} 2>&1)"; then
    case "${app_url}" in
      *"does not exist"*)
        echo "check-deploy-prerequisites: no server on '${STACK}' yet; nothing to check"
        return 0
        ;;
      *)
        fail "could not look up the app URL of '${STACK}': ${app_url}"
        ;;
    esac
  fi
  if [ -z "${app_url}" ] || [ "${app_url}" = "None" ]; then
    echo "check-deploy-prerequisites: no server on '${STACK}' yet; nothing to check"
    return 0
  fi

  if ! health="$(curl --silent --show-error --fail --max-time 20 \
        "${app_url%/}${API_PREFIX}/health" 2>&1)"; then
    fail "could not read ${app_url%/}${API_PREFIX}/health: ${health}"
  fi
  if ! supports="$(printf '%s' "${health}" | python3 -c \
        'import json, sys; print(json.load(sys.stdin)["auth"].get("supports_required_group") is True)' \
        2>/dev/null)"; then
    fail "${app_url%/}${API_PREFIX}/health did not answer with an auth block: ${health}"
  fi

  if [ "${supports}" != "True" ]; then
    fail "the server on '${STACK}' does not enforce an access group yet, and this release moves it to the shared user pool anyone can sign up to. Deploy the release that adds NIMBUS_AUTH_REQUIRED_GROUP (#9) successfully, then retry."
  fi

  echo "check-deploy-prerequisites: server on '${STACK}' enforces an access group; ok"
}

check_worker_handler
check_server_enforces_group
