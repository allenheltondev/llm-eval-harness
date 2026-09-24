"""``scripts/check-deploy-prerequisites.sh``: a deploy the running stack is not ready for stops.

Driven against a fake ``aws`` on ``PATH`` that answers from a small script, so
every branch -- no stack, no worker, the right and the wrong handler, and the
failures to read either -- is exercised without an AWS account.
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

#: A fake `aws`: `describe-stack-resource` and `get-function-configuration`
#: answer from FAKE_* variables (stdout, and an exit code), and every call is
#: logged so a test can see what was asked.
FAKE_AWS = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$2" in
  describe-stack-resource)
    echo "$FAKE_RESOURCE_OUT"; exit "${FAKE_RESOURCE_CODE:-0}" ;;
  get-function-configuration)
    echo "$FAKE_HANDLER_OUT"; exit "${FAKE_HANDLER_CODE:-0}" ;;
esac
exit 99
"""


@pytest.fixture
def aws(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(0o755)
    log = tmp_path / "aws.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_RESOURCE_OUT": "llm-eval-harness-EvalWorkerFunction-abc",
        "FAKE_HANDLER_OUT": "nimbus.worker.lambda_app.handler",
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
    assert "nothing to check" in result.stdout
    assert len(result.calls) == 1  # never asked Lambda


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
    assert all("--region us-east-1" in call for call in result.calls)


def test_deploy_backend_runs_the_check_first():
    makefile = (SCRIPT.parent.parent / "Makefile").read_text()
    recipe = makefile.split("\ndeploy-backend:\n", 1)[1]

    first_command = recipe.splitlines()[1].strip()
    assert first_command.startswith(
        "DEPLOY_REGION=$(DEPLOY_REGION) ./scripts/check-deploy-prerequisites.sh"
    )
