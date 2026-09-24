"""Getting started with ``nimbus``: version, logging, setup hints, init and doctor."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import NamedTuple

import pytest

from nimbus import providers
from nimbus.cli import commands, diagnose
from nimbus.cli import main as cli_main
from nimbus.cli.main import main
from nimbus.config import Settings
from nimbus.models_catalog import CatalogResult, describe_error
from tests.test_cli import _Stdin


class Result(NamedTuple):
    code: int
    out: str
    err: str


def run_cli(*argv: str) -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdin=_Stdin("", tty=True), stdout=out, stderr=err)
    return Result(code, out.getvalue(), err.getvalue())


NOTHING_CONFIGURED = {
    "bedrock": {"configured": False},
    "anthropic": {"configured": False},
    "openai": {"configured": False},
    "ollama": {"configured": False, "reachable": False},
}


def catalog(models=(), providers_block=None, bedrock_error=None) -> CatalogResult:
    return CatalogResult(
        models=list(models),
        providers=providers_block or NOTHING_CONFIGURED,
        cached=False,
        bedrock_error=bedrock_error,
    )


@pytest.fixture
def listing(monkeypatch):
    """What the provider catalog lists; set ``listing.result`` in a test."""

    class Listing:
        result = catalog()

    async def collect(self, settings, bedrock):
        return Listing.result

    monkeypatch.setattr(commands.ProviderCatalog, "collect", collect)
    return Listing


@pytest.fixture
def aws(monkeypatch):
    """The AWS credential status; ``missing`` unless a test says otherwise."""

    class Aws:
        status = "missing"

    monkeypatch.setattr(providers, "aws_credentials_status", lambda: Aws.status)
    return Aws


def repo_root() -> Path:
    return next(p for p in Path(__file__).resolve().parents if (p / "docs").is_dir())


# --------------------------------------------------------------------------- #
# --version, --verbose
# --------------------------------------------------------------------------- #


def test_version_names_the_installed_distribution(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"nimbus {cli_main.version()}\n"


def test_an_uninstalled_tree_reports_dev(monkeypatch):
    def not_installed(name):
        raise cli_main.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(cli_main.metadata, "version", not_installed)

    assert cli_main.version() == "dev"


@pytest.fixture
def noisy_command(monkeypatch):
    async def noisy(args, settings, out, err):
        logging.getLogger("nimbus.somewhere").info("an info line")
        logging.getLogger("nimbus.somewhere").warning("a warning line")
        logging.getLogger("nimbus.somewhere").error("an error line")
        return 0

    monkeypatch.setitem(cli_main.COMMANDS, "tools", noisy)


def test_library_warnings_stay_quiet_but_errors_show(noisy_command):
    result = run_cli("tools")

    assert "a warning line" not in result.err
    assert "an info line" not in result.err
    assert "| log ERROR nimbus.somewhere: an error line" in result.err


def test_verbose_shows_the_log(noisy_command):
    result = run_cli("tools", "-v")

    assert "| log INFO nimbus.somewhere: an info line" in result.err
    assert "| log WARNING nimbus.somewhere: a warning line" in result.err


def test_repeated_invocations_never_stack_log_handlers(noisy_command):
    first, second = io.StringIO(), io.StringIO()

    main(["tools"], stdin=_Stdin(""), stdout=io.StringIO(), stderr=first)
    main(["tools"], stdin=_Stdin(""), stdout=io.StringIO(), stderr=second)

    # The first call's handler is gone: nothing from the second call reaches it.
    assert first.getvalue().count("an error line") == 1
    assert second.getvalue().count("an error line") == 1


# --------------------------------------------------------------------------- #
# models, explained
# --------------------------------------------------------------------------- #


def test_models_says_what_is_broken_and_what_to_set(listing, aws):
    aws.status = "ok"
    listing.result = catalog(
        providers_block={
            **NOTHING_CONFIGURED,
            "ollama": {"configured": True, "reachable": False},
        },
        bedrock_error="ExpiredTokenException: the security token has expired",
    )

    result = run_cli("--local", "models")

    assert result.out == ""
    assert "| bedrock: ExpiredTokenException" in result.err
    assert "|   fix: your AWS session expired: `aws sso login`" in result.err
    assert "| ollama: not answering at" in result.err
    assert "|   fix: start it with `ollama serve`" in result.err
    assert "not configured: anthropic (set ANTHROPIC_API_KEY), openai (set OPENAI_API_KEY)" in (
        result.err
    )
    assert "| no models available: `nimbus doctor` checks your setup" in result.err


def test_models_lists_without_hints_when_everything_works(listing, aws):
    aws.status = "ok"
    listing.result = catalog(
        models=[{"source": "bedrock", "model_id": "m1", "name": "One"}],
        providers_block={**NOTHING_CONFIGURED, "bedrock": {"configured": True}},
    )

    result = run_cli("models")

    assert "m1" in result.out
    assert "fix:" not in result.err
    assert "no models available" not in result.err


# --------------------------------------------------------------------------- #
# diagnose
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "error", "expected", "fix"),
    [
        ("missing", None, "off", "aws configure"),
        ("error", None, "fail", "AWS_PROFILE"),
        ("ok", "ExpiredTokenException: expired", "fail", "aws sso login"),
        ("ok", "AccessDeniedException: no", "fail", "bedrock:ListFoundationModels"),
        ("ok", "UnrecognizedClientException: bad", "fail", "aws sts get-caller-identity"),
        ("ok", None, "ok", None),
    ],
)
def test_bedrock_check(aws, status, error, expected, fix):
    aws.status = status
    listed = catalog(
        models=[{"source": "bedrock", "model_id": "m1"}] if error is None else [],
        bedrock_error=error,
    )

    check = diagnose.bedrock_check(Settings(aws_region="eu-west-1"), listed)

    assert check.status == expected
    if fix:
        assert fix in check.fix
    else:
        assert check.detail == "1 model in eu-west-1"


def test_provider_checks_cover_every_provider(aws):
    aws.status = "ok"
    listed = catalog(
        models=[
            {"source": "bedrock", "model_id": "b1"},
            {"source": "bedrock", "model_id": "b2"},
            {"source": "anthropic", "model_id": "a1"},
        ],
        providers_block={
            "bedrock": {"configured": True},
            "anthropic": {"configured": True},
            "openai": {"configured": True},
            "ollama": {"configured": True, "reachable": False},
        },
    )
    settings = Settings(ollama_base_url="localhost:11434")

    checks = {check.name: check for check in diagnose.provider_checks(settings, listed)}

    assert checks["bedrock"].detail == "2 models in us-east-1"
    assert (checks["anthropic"].status, checks["anthropic"].detail) == ("ok", "1 model")
    assert checks["openai"].status == "fail"
    assert checks["openai"].fix == "check OPENAI_API_KEY"
    assert checks["ollama"].status == "fail"
    assert checks["ollama"].detail == "not answering at http://localhost:11434"


def test_history_check(monkeypatch, tmp_path):
    local = Settings(db_path=str(tmp_path / "h.db"))
    assert diagnose.history_check(local) == diagnose.Check("history", "ok", str(tmp_path / "h.db"))
    assert diagnose.history_check(Settings(db_path=":memory:")).status == "ok"

    table = Settings(history_backend="dynamodb", eval_table="t")
    assert diagnose.history_check(table).detail == "DynamoDB t"
    assert diagnose.history_check(Settings(history_backend="dynamodb")).status == "fail"

    monkeypatch.setattr(diagnose.os, "access", lambda path, mode: False)
    blocked = diagnose.history_check(local)
    assert blocked.status == "fail"
    assert "NIMBUS_DB_PATH" in blocked.fix


def test_errors_are_described_in_one_line():
    class ClientError(Exception):
        response = {"Error": {"Code": "Throttled", "Message": "slow down"}}

    assert describe_error(ClientError()) == "Throttled: slow down"
    assert describe_error(RuntimeError("boom")) == "boom"
    assert describe_error(RuntimeError()) == "RuntimeError"


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_the_starter_suite_is_the_documented_example():
    documented = (repo_root() / "docs" / "examples" / "support-suite.yaml").read_text()

    assert commands.starter_suite() == documented


def test_init_writes_a_suite_that_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NIMBUS_FAKE_MODEL", "1")

    written = run_cli("init")
    ran = run_cli("--db", str(tmp_path / "h.db"), "eval", "--suite", "suite.yaml")

    assert written.code == 0, written.err
    assert "| run it:   nimbus eval --suite suite.yaml" in written.err
    assert ran.code == 0, ran.err
    assert len(json.loads(ran.out)["result"]["cases"]) == 4


def test_init_puts_in_the_model_asked_for(tmp_path):
    target = tmp_path / "evals" / "mine.yaml"

    result = run_cli("init", str(target), "--model", "anthropic.claude-x", "--json")

    assert json.loads(result.out) == {"path": str(target)}
    assert "  model_id: anthropic.claude-x\n" in target.read_text()
    assert "amazon.nova-lite" not in target.read_text()


def test_init_never_replaces_a_file_unless_forced(tmp_path):
    target = tmp_path / "suite.yaml"
    target.write_text("mine")

    refused = run_cli("init", str(target))
    assert refused.code == 1
    assert "already exists; pass --force" in refused.err
    assert target.read_text() == "mine"

    assert run_cli("init", str(target), "--force").code == 0
    assert target.read_text() == commands.starter_suite()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_with_nothing_set_up_says_what_to_do(listing, aws):
    result = run_cli("doctor")

    assert result.code == 1
    lines = result.out.splitlines()
    assert lines[0].startswith("nimbus ")
    assert any(line.startswith("ok  history") for line in lines)
    assert any(line.startswith("--  bedrock") for line in lines)
    assert "-> `aws configure` or `aws sso login`, or set AWS_PROFILE" in result.out
    assert "-> `nimbus login --url https://<your-stack>`" in result.out
    assert "!!  models" in result.out
    assert result.err == "| 1 problem\n"


def test_doctor_is_happy_with_one_working_provider(listing, aws):
    aws.status = "ok"
    listing.result = catalog(models=[{"source": "bedrock", "model_id": "m1"}])

    result = run_cli("doctor")

    assert result.code == 0
    assert "ok  bedrock    1 model in us-east-1" in result.out
    assert "!!" not in result.out
    assert result.err == "| all good\n"


def test_doctor_reports_a_configured_provider_that_is_broken(listing, aws):
    aws.status = "ok"
    listing.result = catalog(bedrock_error="UnrecognizedClientException: bad token")

    result = run_cli("doctor")

    assert result.code == 1
    assert "!!  bedrock    UnrecognizedClientException: bad token" in result.out
    assert result.err == "| 2 problems\n"


def test_doctor_as_json(listing, aws):
    result = run_cli("doctor", "--json")

    payload = json.loads(result.out)
    assert payload["ok"] is False
    assert payload["version"] == cli_main.version()
    names = [check["name"] for check in payload["checks"]]
    assert names == ["history", "bedrock", "anthropic", "openai", "ollama", "stack", "models"]
    assert payload["checks"][5]["status"] == "off"


def test_verbose_lowers_a_quieter_root_level(noisy_command):
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.WARNING)
    try:
        result = run_cli("tools", "-v")
        assert "an info line" in result.err
        assert root.level == logging.INFO
    finally:
        root.setLevel(before)


def test_a_server_timestamp_is_shown_to_the_second():
    assert commands._when("2026-09-24T10:00:00.123456+00:00") == "2026-09-24T10:00:00+00:00"
    assert commands._when("yesterday") == "yesterday"
