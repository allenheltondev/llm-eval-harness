"""``scripts/check-deploy-prerequisites.sh``: a deploy the running stack is not ready for stops.

Driven against a fake ``aws`` and ``curl`` on ``PATH`` that answer from small
scripts, so every branch -- no stack, no worker, the right and the wrong
handler, a live server that does or does not enforce an access group, and the
failures to read any of it -- is exercised without an AWS account.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest


def _find_script() -> pathlib.Path:
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "check-deploy-prerequisites.sh"
        if candidate.is_file():
            return candidate
    raise AssertionError("could not find scripts/check-deploy-prerequisites.sh")


SCRIPT = _find_script()

#: A fake `aws`: `describe-stack-resource`, `get-function-configuration` and
#: `describe-stacks` answer from FAKE_* variables (stdout, and an exit code),
#: and every call is logged so a test can see what was asked.
FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$2" in
  describe-stack-resource)
    echo "$FAKE_RESOURCE_OUT"; exit "${FAKE_RESOURCE_CODE:-0}" ;;
  get-function-configuration)
    echo "$FAKE_HANDLER_OUT"; exit "${FAKE_HANDLER_CODE:-0}" ;;
  describe-stacks)
    echo "$FAKE_APP_URL_OUT"; exit "${FAKE_APP_URL_CODE:-0}" ;;
esac
exit 99
"""

#: A fake `curl`: answers the live /health from FAKE_HEALTH_* the same way.
FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
echo "$FAKE_HEALTH_OUT"; exit "${FAKE_HEALTH_CODE:-0}"
"""

GROUP_ENFORCED = (
    '{"status": "ok", "auth": {"required": true, "required_group": null,'
    ' "supports_required_group": true}}'
)


@pytest.fixture
def aws(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("aws", FAKE_AWS), ("curl", FAKE_CURL)):
        fake = bin_dir / name
        fake.write_text(script)
        fake.chmod(0o755)
    log = tmp_path / "aws.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_RESOURCE_OUT": "llm-eval-harness-EvalWorkerFunction-abc",
        "FAKE_HANDLER_OUT": "nimbus.worker.lambda_app.handler",
        "FAKE_APP_URL_OUT": "https://d111.cloudfront.net",
        "FAKE_HEALTH_OUT": GROUP_ENFORCED,
    }
    env.pop("DEPLOY_REGION", None)

    def run(**overrides: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(SCRIPT), "llm-eval-harness"],
            env={**env, **overrides},
            capture_output=True,
            text=True,
            check=False,
        )
        result.calls = log.read_text().splitlines() if log.exists() else []  # type: ignore[attr-defined]
        return result

    return run


def test_a_worker_already_on_the_nimbus_handler_may_deploy(aws):
    result = aws()

    assert result.returncode == 0, result.stderr
    assert "runs nimbus.worker.lambda_app.handler; ok" in result.stdout
    assert "--function-name llm-eval-harness-EvalWorkerFunction-abc" in result.calls[1]


def test_a_worker_still_on_the_old_handler_stops_the_deploy(aws):
    result = aws(FAKE_HANDLER_OUT="evalharness.worker.lambda_app.handler")

    assert result.returncode == 1
    assert "still runs Handler 'evalharness.worker.lambda_app.handler'" in result.stderr
    assert "nimbus.worker.lambda_app.handler to be live first" in result.stderr


@pytest.mark.parametrize(
    "message",
    [
        "Stack with id llm-eval-harness does not exist",
        "Resource EvalWorkerFunction does not exist for stack llm-eval-harness",
    ],
)
def test_nothing_deployed_yet_has_nothing_to_check(aws, message):
    result = aws(FAKE_RESOURCE_OUT=message, FAKE_RESOURCE_CODE="254")

    assert result.returncode == 0, result.stderr
    assert "no eval worker" in result.stdout
    assert not any("get-function-configuration" in call for call in result.calls)


def test_a_stack_it_cannot_read_stops_the_deploy(aws):
    result = aws(
        FAKE_RESOURCE_OUT="An error occurred (AccessDenied) when calling DescribeStackResource",
        FAKE_RESOURCE_CODE="254",
    )

    assert result.returncode == 1
    assert "could not look up the eval worker" in result.stderr
    assert "AccessDenied" in result.stderr


def test_a_handler_it_cannot_read_stops_the_deploy(aws):
    result = aws(
        FAKE_HANDLER_OUT="An error occurred (AccessDeniedException): not authorized",
        FAKE_HANDLER_CODE="254",
    )

    assert result.returncode == 1
    assert "could not read the eval worker's handler" in result.stderr


def test_the_region_is_passed_through_when_given(aws):
    result = aws(DEPLOY_REGION="us-east-1")

    assert result.returncode == 0, result.stderr
    aws_calls = [call for call in result.calls if call.startswith("aws ")]
    assert len(aws_calls) == 3
    assert all("--region us-east-1" in call for call in aws_calls)


def test_a_server_that_enforces_the_access_group_may_deploy(aws):
    result = aws()

    assert result.returncode == 0, result.stderr
    assert "enforces an access group; ok" in result.stdout
    assert result.calls[-1].endswith("https://d111.cloudfront.net/api/v1/health")


@pytest.mark.parametrize(
    "health",
    [
        # Before NIMBUS_AUTH_REQUIRED_GROUP existed.
        '{"status": "ok", "auth": {"required": true, "region": "us-east-1"}}',
        '{"status": "ok", "auth": {"required": false}}',
        '{"status": "ok", "auth": {"required": false, "supports_required_group": false}}',
    ],
)
def test_a_server_that_ignores_the_access_group_stops_the_deploy(aws, health):
    result = aws(FAKE_HEALTH_OUT=health)

    assert result.returncode == 1
    assert "does not enforce an access group yet" in result.stderr
    assert "(#9)" in result.stderr


def test_a_stack_with_no_server_yet_has_no_group_to_check(aws):
    for app_url in ("None", ""):
        result = aws(FAKE_APP_URL_OUT=app_url)

        assert result.returncode == 0, result.stderr
        assert "no server on 'llm-eval-harness' yet" in result.stdout
        assert not any(call.startswith("curl ") for call in result.calls)


def test_no_stack_at_all_passes_both_checks(aws):
    result = aws(
        FAKE_RESOURCE_OUT="Stack with id llm-eval-harness does not exist",
        FAKE_RESOURCE_CODE="254",
        FAKE_APP_URL_OUT="Stack with id llm-eval-harness does not exist",
        FAKE_APP_URL_CODE="254",
    )

    assert result.returncode == 0, result.stderr
    assert "no server on 'llm-eval-harness' yet" in result.stdout


def test_an_app_url_it_cannot_read_stops_the_deploy(aws):
    result = aws(
        FAKE_APP_URL_OUT="An error occurred (Throttling) when calling DescribeStacks",
        FAKE_APP_URL_CODE="254",
    )

    assert result.returncode == 1
    assert "could not look up the app URL" in result.stderr


def test_a_server_that_does_not_answer_stops_the_deploy(aws):
    result = aws(
        FAKE_HEALTH_OUT="curl: (22) The requested URL returned error: 502", FAKE_HEALTH_CODE="22"
    )

    assert result.returncode == 1
    assert "could not read https://d111.cloudfront.net/api/v1/health" in result.stderr


@pytest.mark.parametrize("health", ["<html>not json</html>", '{"status": "ok"}'])
def test_a_health_answer_without_an_auth_block_stops_the_deploy(aws, health):
    result = aws(FAKE_HEALTH_OUT=health)

    assert result.returncode == 1
    assert "did not answer with an auth block" in result.stderr


def test_deploy_backend_runs_the_check_first():
    makefile = (SCRIPT.parent.parent / "Makefile").read_text()
    recipe = makefile.split("\ndeploy-backend:\n", 1)[1]

    first_command = recipe.splitlines()[1].strip()
    assert first_command.startswith(
        "DEPLOY_REGION=$(DEPLOY_REGION) ./scripts/check-deploy-prerequisites.sh"
    )


def _deploy_overrides(*make_args: str) -> str:
    """The `sam deploy` line `make deploy-backend` would run, without running it."""
    result = subprocess.run(
        ["make", "-n", "-C", str(SCRIPT.parent.parent), "deploy-backend", *make_args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_deploy_backend_passes_an_access_group_name_when_given():
    assert '"AccessGroupName=nimbus-team"' in _deploy_overrides("ACCESS_GROUP_NAME=nimbus-team")


def test_deploy_backend_leaves_the_access_group_to_the_template_by_default():
    assert "AccessGroupName" not in _deploy_overrides()


def test_deploy_backend_passes_a_custom_domain_when_given():
    overrides = _deploy_overrides("APP_DOMAIN_NAME=nimbus.example.com", "APP_HOSTED_ZONE_ID=Z123")
    assert '"AppDomainName=nimbus.example.com"' in overrides
    assert '"AppHostedZoneId=Z123"' in overrides


def test_deploy_backend_leaves_the_domain_off_by_default():
    overrides = _deploy_overrides()
    assert "AppDomainName" not in overrides
    assert "AppHostedZoneId" not in overrides
