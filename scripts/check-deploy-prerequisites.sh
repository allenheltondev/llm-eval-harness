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
# Current prerequisite: the eval worker must already be running with
# Handler nimbus.worker.lambda_app.handler. This release's worker zip no longer
# carries the pre-rename `evalharness` shim, so deploying it onto a worker still
# configured with the old handler would leave the function unable to import
# its handler until the configuration update lands.
#
# Fails closed: anything it cannot read (a permission, a throttle) stops the
# deploy with the reason, rather than being taken as "fine". A stack or worker
# that does not exist yet has nothing to be incompatible with, and passes.
#
# Usage: scripts/check-deploy-prerequisites.sh STACK_NAME   (honours DEPLOY_REGION)

set -euo pipefail

STACK="${1:?usage: check-deploy-prerequisites.sh STACK_NAME}"
REQUIRED_HANDLER="nimbus.worker.lambda_app.handler"
REGION_ARGS=()
if [ -n "${DEPLOY_REGION:-}" ]; then
  REGION_ARGS=(--region "${DEPLOY_REGION}")
fi

fail() {
  echo "check-deploy-prerequisites: $*" >&2
  exit 1
}

if ! resource_out="$(aws cloudformation describe-stack-resource \
      --stack-name "${STACK}" \
      --logical-resource-id EvalWorkerFunction \
      --query StackResourceDetail.PhysicalResourceId \
      --output text ${REGION_ARGS[@]+"${REGION_ARGS[@]}"} 2>&1)"; then
  case "${resource_out}" in
    *"does not exist"*)
      echo "check-deploy-prerequisites: no eval worker on '${STACK}' yet; nothing to check"
      exit 0
      ;;
    *)
      fail "could not look up the eval worker on '${STACK}': ${resource_out}"
      ;;
  esac
fi
function_name="${resource_out}"

if ! handler="$(aws lambda get-function-configuration \
      --function-name "${function_name}" \
      --query Handler \
      --output text ${REGION_ARGS[@]+"${REGION_ARGS[@]}"} 2>&1)"; then
  fail "could not read the eval worker's handler (${function_name}): ${handler}"
fi

if [ "${handler}" != "${REQUIRED_HANDLER}" ]; then
  fail "the eval worker on '${STACK}' still runs Handler '${handler}'. This release needs ${REQUIRED_HANDLER} to be live first: deploy the release that switches it (#7) successfully, then retry."
fi

echo "check-deploy-prerequisites: eval worker on '${STACK}' runs ${REQUIRED_HANDLER}; ok"
