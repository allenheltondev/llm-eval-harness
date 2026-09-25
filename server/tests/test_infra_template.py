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
# After the rename's overlap release
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("logical_id", ["ServerFunction", "EvalWorkerFunction"])
def test_no_setting_is_given_under_its_pre_rename_name(resources: dict, logical_id: str) -> None:
    """The overlap release has shipped everywhere: one name per setting again."""
    env = _env(resources, logical_id)

    assert not [name for name in env if name.startswith("EVALHARNESS_")]


def test_the_worker_handler_is_the_nimbus_worker(resources: dict) -> None:
    """The pre-rename ``evalharness`` shim is gone, so nothing else would import."""
    assert (
        resources["EvalWorkerFunction"]["Properties"]["Handler"]
        == "nimbus.worker.lambda_app.handler"
    )


def test_the_server_verifies_tokens_from_the_shared_pool(template: dict, resources: dict) -> None:
    """Sign-in is the Ready, Set, Cloud pool that rsc-core publishes in SSM."""
    parameter = template["Parameters"]["AuthUserPoolId"]
    assert parameter["Type"] == "AWS::SSM::Parameter::Value<String>"
    assert parameter["Default"] == "/readysetcloud/auth/user-pool-id"

    env = _env(resources, "ServerFunction")
    assert env["NIMBUS_AUTH_USER_POOL_ID"] == {"Fn::Ref": "AuthUserPoolId"}
    assert env["NIMBUS_AUTH_CLIENT_ID"] == {"Fn::Ref": "AuthClient"}
    assert resources["AuthClient"]["Properties"]["UserPoolId"] == {"Fn::Ref": "AuthUserPoolId"}


def test_an_account_in_the_shared_pool_is_not_access_without_the_group(resources: dict) -> None:
    """Anyone can sign up to the shared pool; the group is what grants this stack."""
    env = _env(resources, "ServerFunction")
    assert env["NIMBUS_AUTH_REQUIRED_GROUP"] == {"Fn::Ref": "AccessGroup"}

    group = resources["AccessGroup"]
    assert group["Type"] == "AWS::Cognito::UserPoolGroup"
    assert group["Properties"]["UserPoolId"] == {"Fn::Ref": "AuthUserPoolId"}


@pytest.mark.parametrize("logical_id", ["UserPool", "UserPoolClient"])
def test_the_stacks_own_pool_is_retained_with_its_accounts(
    resources: dict, logical_id: str
) -> None:
    assert resources[logical_id]["DeletionPolicy"] == "Retain"
    assert resources[logical_id]["UpdateReplacePolicy"] == "Retain"


def test_the_access_group_is_named_after_the_stack_unless_told_otherwise(
    template: dict, resources: dict
) -> None:
    """Group names are shared by every stack on the pool; stack names are unique."""
    assert template["Parameters"]["AccessGroupName"]["Default"] == ""
    assert template["Conditions"]["HasAccessGroupName"] == {
        "Fn::Not": [{"Fn::Equals": [{"Fn::Ref": "AccessGroupName"}, ""]}]
    }
    assert resources["AccessGroup"]["Properties"]["GroupName"] == {
        "Fn::If": [
            "HasAccessGroupName",
            {"Fn::Ref": "AccessGroupName"},
            {"Fn::Ref": "AWS::StackName"},
        ]
    }


def test_the_custom_domain_is_off_unless_both_parameters_are_given(template: dict) -> None:
    """Every deploy but production passes neither, and serves on *.cloudfront.net."""
    parameters = template["Parameters"]
    assert parameters["AppDomainName"]["Default"] == ""
    assert parameters["AppHostedZoneId"]["Default"] == ""
    assert template["Conditions"]["DeployCustomDomain"] == {
        "Fn::And": [
            {"Fn::Condition": "DeployServer"},
            {"Fn::Not": [{"Fn::Equals": [{"Fn::Ref": "AppDomainName"}, ""]}]},
            {"Fn::Not": [{"Fn::Equals": [{"Fn::Ref": "AppHostedZoneId"}, ""]}]},
        ]
    }


def test_the_distribution_serves_the_domain_on_its_own_dns_validated_certificate(
    resources: dict,
) -> None:
    certificate = resources["AppCertificate"]
    assert certificate["Condition"] == "DeployCustomDomain"
    assert certificate["Properties"]["ValidationMethod"] == "DNS"
    assert certificate["Properties"]["DomainValidationOptions"] == [
        {"DomainName": {"Fn::Ref": "AppDomainName"}, "HostedZoneId": {"Fn::Ref": "AppHostedZoneId"}}
    ]

    config = resources["AppDistribution"]["Properties"]["DistributionConfig"]
    assert config["Aliases"] == {
        "Fn::If": [
            "DeployCustomDomain",
            [{"Fn::Ref": "AppDomainName"}],
            {"Fn::Ref": "AWS::NoValue"},
        ]
    }
    on, off = config["ViewerCertificate"]["Fn::If"][1:]
    assert on["AcmCertificateArn"] == {"Fn::Ref": "AppCertificate"}
    assert on["SslSupportMethod"] == "sni-only"
    assert off == {"CloudFrontDefaultCertificate": True}


@pytest.mark.parametrize(
    "logical_id, record_type", [("AppDnsRecord", "A"), ("AppDnsRecordIpv6", "AAAA")]
)
def test_the_domain_points_at_the_distribution(
    resources: dict, logical_id: str, record_type: str
) -> None:
    record = resources[logical_id]
    assert record["Condition"] == "DeployCustomDomain"
    properties = record["Properties"]
    assert properties["Type"] == record_type
    assert properties["Name"] == {"Fn::Ref": "AppDomainName"}
    assert properties["AliasTarget"]["DNSName"] == {"Fn::GetAtt": "AppDistribution.DomainName"}
    # CloudFront's fixed alias hosted zone.
    assert properties["AliasTarget"]["HostedZoneId"] == "Z2FDTNDATAQYW2"


def test_the_app_url_is_the_custom_domain_when_there_is_one(template: dict) -> None:
    assert template["Outputs"]["AppUrl"]["Value"] == {
        "Fn::If": [
            "DeployCustomDomain",
            {"Fn::Sub": "https://${AppDomainName}"},
            {"Fn::Sub": "https://${AppDistribution.DomainName}"},
        ]
    }


def _workflow(name: str) -> str:
    return (TEMPLATE.parent.parent / ".github" / "workflows" / name).read_text()


def test_only_production_deploys_the_custom_domain() -> None:
    production = _workflow("deploy.yaml")
    assert "APP_DOMAIN_NAME: nimbus.readysetcloud.io" in production
    assert "APP_HOSTED_ZONE_ID: ${{ vars.HOSTED_ZONE_ID }}" in production

    staging = _workflow("pull-request.yaml")
    assert "APP_DOMAIN_NAME" not in staging
    assert "APP_HOSTED_ZONE_ID" not in staging
