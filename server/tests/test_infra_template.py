"""Assertions about ``infra/template.yaml`` that only a deploy would otherwise catch.

``sam validate --lint`` checks that the template is well formed. It cannot
check that the *values* wire the application up correctly, and the two bugs
this file guards against both deployed clean and broke the thing they
configured:

* the eval worker inherited its environment from the AgentCore runtime, where
  ``history_backend: auto`` resolved to SQLite. Lambda sets
  ``AWS_LAMBDA_FUNCTION_NAME``, so the same environment resolves to DynamoDB --
  and the worker has no ``EVALHARNESS_EVAL_TABLE``, so every evaluation died in
  ``get_history_repo()`` before running anything.
* the server's Function URL had no resource policy, so every request 403'd
  (SAM only emits one for a *literal* ``AuthType: NONE``).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / "infra" / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    """A loader that tolerates CloudFormation's short intrinsic tags.

    ``!Ref``/``!GetAtt``/``!Sub`` and friends are not YAML the safe loader
    knows. Nothing here inspects an intrinsic's meaning -- the assertions are
    about plain scalars -- so they are read back as opaque markers.
    """


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict[str, object]:
    if isinstance(node, yaml.ScalarNode):
        value: object = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {f"Fn::{tag_suffix}": value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def resources(template: dict) -> dict:
    return template["Resources"]


def _env(resources: dict, logical_id: str) -> dict[str, object]:
    return resources[logical_id]["Properties"]["Environment"]["Variables"]


def test_worker_pins_sqlite_rather_than_letting_auto_resolve(resources: dict) -> None:
    """The trap: inside Lambda, ``auto`` means DynamoDB, and the worker has no table."""
    env = _env(resources, "EvalWorkerFunction")
    assert env["EVALHARNESS_HISTORY_BACKEND"] == "sqlite"
    assert env["EVALHARNESS_DB_PATH"].startswith("/tmp/")
    # Pinning is only correct *because* the worker has no table of its own to
    # point the history repo at; its DynamoDB writes go through DynamoEvalStore.
    assert "EVALHARNESS_EVAL_TABLE" not in env


def test_server_keeps_its_own_backend_explicit(resources: dict) -> None:
    """The server is the half that really does use DynamoDB for history."""
    env = _env(resources, "ServerFunction")
    assert env["EVALHARNESS_HISTORY_BACKEND"] == "dynamodb"
    assert "EVALHARNESS_EVAL_TABLE" in env


def test_function_url_has_a_public_resource_policy(resources: dict, template: dict) -> None:
    """``AuthType: NONE`` is not reachable on its own; SAM does not add this under a !Ref."""
    permission = resources["ServerFunctionUrlPublicPermission"]
    assert permission["Type"] == "AWS::Lambda::Permission"
    assert permission["Properties"]["Action"] == "lambda:InvokeFunctionUrl"
    assert permission["Properties"]["Principal"] == "*"
    assert permission["Properties"]["FunctionUrlAuthType"] == "NONE"
    # And it follows the parameter, so flipping to AWS_IAM does not leave a
    # public policy behind.
    assert permission["Condition"] == "ServerFunctionUrlIsPublic"
    assert "ServerFunctionUrlIsPublic" in template["Conditions"]


def test_worker_never_retries_an_async_invocation(resources: dict) -> None:
    """A retry can only mean the invocation died; re-running burns another 15 minutes."""
    assert resources["EvalWorkerFunction"]["Properties"]["EventInvokeConfig"][
        "MaximumRetryAttempts"
    ] == 0
