"""Assertions about ``infra/template.yaml`` that only a deploy would otherwise catch.

``sam validate --lint`` checks that the template is well formed. It cannot
check that the *values* wire the application up correctly, and the two bugs
this file guards against both deployed clean and broke the thing they
configured:

* the eval worker inherited its environment from the AgentCore runtime, where
  ``history_backend: auto`` resolved to SQLite. Lambda sets
  ``AWS_LAMBDA_FUNCTION_NAME``, so the same environment resolves to DynamoDB --
  and the worker has no ``NIMBUS_EVAL_TABLE``, so every evaluation died in
  ``get_history_repo()`` before running anything.
* the server's Function URL had no resource policy, so every request 403'd
  (SAM only emits one for a *literal* ``AuthType: NONE``).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


def _find_template() -> pathlib.Path:
    """Locate ``infra/template.yaml`` by walking up, not by counting parents.

    A fixed ``parents[2]`` is wrong under mutmut, which copies ``tests/`` into
    ``server/mutants/`` and so adds a level. That shifts the path to
    ``server/infra/template.yaml``, the fixture raises, and because mutmut aborts
    when its baseline run fails, the whole mutation job dies at collection
    rather than reporting a single broken test.
    """
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "infra" / "template.yaml"
        if candidate.is_file():
            return candidate
    raise AssertionError("could not find infra/template.yaml above this test")


TEMPLATE = _find_template()


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
    assert env["NIMBUS_HISTORY_BACKEND"] == "sqlite"
    assert env["NIMBUS_DB_PATH"].startswith("/tmp/")
    # Pinning is only correct *because* the worker has no table of its own to
    # point the history repo at; its DynamoDB writes go through DynamoEvalStore.
    assert "NIMBUS_EVAL_TABLE" not in env


def test_server_keeps_its_own_backend_explicit(resources: dict) -> None:
    """The server is the half that really does use DynamoDB for history."""
    env = _env(resources, "ServerFunction")
    assert env["NIMBUS_HISTORY_BACKEND"] == "dynamodb"
    assert "NIMBUS_EVAL_TABLE" in env


def test_history_retention_reaches_both_writers_and_defaults_to_forever(
    resources: dict, template: dict
) -> None:
    """The server and the worker both write history items; one rule, one parameter."""
    parameter = template["Parameters"]["HistoryRetentionDays"]
    assert parameter["Default"] == 0
    assert parameter["MinValue"] == 0
    for function in ("ServerFunction", "EvalWorkerFunction"):
        env = _env(resources, function)
        assert env["NIMBUS_HISTORY_RETENTION_DAYS"] == {"Fn::Ref": "HistoryRetentionDays"}


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


def test_worker_retries_an_invocation_that_fails_before_claiming(resources: dict) -> None:
    """The recovery path for a failure before the claim, where the handler raises.

    With retries off, raising would change nothing: Lambda would drop the event
    and the server's `pending` row would never move. Retries are safe only
    because a redelivery of a claimed evaluation runs nothing -- so this is on,
    and bounded.
    """
    retries = resources["EvalWorkerFunction"]["Properties"]["EventInvokeConfig"][
        "MaximumRetryAttempts"
    ]
    assert 1 <= retries <= 2


# --------------------------------------------------------------------------- #
# The rename's deploy: no window where running code misses its configuration
# --------------------------------------------------------------------------- #

LEGACY_PREFIX = "EVALHARNESS_"


@pytest.mark.parametrize("logical_id", ["ServerFunction", "EvalWorkerFunction"])
def test_every_setting_is_also_given_under_its_pre_rename_name(
    resources: dict, logical_id: str
) -> None:
    """CloudFormation may apply the environment before the code.

    The code still running then reads ``EVALHARNESS_*``; each of its settings
    must be there, with the same value the new code reads as ``NIMBUS_*``.
    """
    env = _env(resources, logical_id)
    current = {
        name.removeprefix("NIMBUS_"): value
        for name, value in env.items()
        if name.startswith("NIMBUS_")
    }
    legacy = {
        name.removeprefix(LEGACY_PREFIX): value
        for name, value in env.items()
        if name.startswith(LEGACY_PREFIX)
    }

    assert current
    assert legacy == current


def test_the_old_server_code_keeps_its_sign_in_gate(resources: dict) -> None:
    """Without these the old code serves every route unauthenticated."""
    env = _env(resources, "ServerFunction")

    assert env["EVALHARNESS_AUTH_USER_POOL_ID"] == {"Fn::Ref": "UserPool"}
    assert env["EVALHARNESS_AUTH_CLIENT_ID"] == {"Fn::Ref": "UserPoolClient"}


def test_the_worker_handler_keeps_its_pre_rename_path(resources: dict) -> None:
    assert (
        resources["EvalWorkerFunction"]["Properties"]["Handler"]
        == "evalharness.worker.lambda_app.handler"
    )


def test_the_pre_rename_handler_is_the_nimbus_worker(monkeypatch) -> None:
    """The shim the worker zip carries resolves to the real handler."""
    import importlib
    import sys

    from nimbus.worker import lambda_app

    compat = TEMPLATE.parent.parent / "server" / "compat"
    monkeypatch.syspath_prepend(str(compat))
    for name in [key for key in sys.modules if key.split(".")[0] == "evalharness"]:
        monkeypatch.delitem(sys.modules, name)

    shim = importlib.import_module("evalharness.worker.lambda_app")

    assert shim.handler is lambda_app.handler


def test_the_worker_zip_carries_the_shim() -> None:
    script = (TEMPLATE.parent.parent / "scripts" / "package-eval-worker.sh").read_text()

    assert 'cp -R "${SERVER}/compat/evalharness" "${STAGING}/evalharness"' in script
