"""Tests for the command-line interface (nimbus.cli).

These drive the real entry point -- ``main(argv, stdin=, stdout=, stderr=)`` --
against the scripted fake model and a tmp_path sqlite file, so a passing test
means the whole path works: argparse, prompt resolution, the engine, the store,
the rendering and the exit code. Nothing here mocks the engine.

The two output streams are asserted separately on purpose. "stdout is the
product, stderr is the commentary" is the CLI's central promise, and it is only
worth anything if something checks it.
"""

import asyncio
import inspect
import io
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

from nimbus.cli import commands
from nimbus.cli.main import COMMANDS, main
from nimbus.engine import runner
from nimbus.engine.fake_model import FakeModel, Text
from nimbus.errors import AppError, NotFoundError
from nimbus.models_catalog import CatalogResult
from nimbus.store import db as store_db
from nimbus.store import repo as store_repo

FAKE_PREFIX = "[fake-model]"


class Result:
    """One CLI invocation: its exit code and what it wrote where."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()

    def json(self):
        return json.loads(self.out)

    def ndjson(self) -> list[dict]:
        return [json.loads(line) for line in self.out.splitlines() if line]


class _Stdin(io.StringIO):
    """A StringIO that can claim to be a terminal."""

    def __init__(self, text: str = "", tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "cli.db")


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    """Every run in this module goes through the scripted model, never AWS."""
    monkeypatch.setenv("NIMBUS_FAKE_MODEL", "1")
    # The sqlite engine is a module-level singleton that outlives a test, so a
    # test asserting on *which* database was opened has to start from unset.
    monkeypatch.setattr(store_db, "_engine", None)
    store_repo.reset_cache()
    # `main()` exports settings into the real os.environ on purpose -- that is
    # the only channel `serve`'s uvicorn child can read -- so a CLI invocation
    # leaves the process environment changed. Snapshot and restore it, or those
    # exports leak into every test that builds Settings() afterwards.
    saved_environ = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved_environ)
    store_repo.reset_cache()


@pytest.fixture
def cli(db_path):
    """Invoke the CLI against this test's database and capture both streams."""

    def _run(*argv: str, stdin: str | None = None, tty: bool = False) -> Result:
        out, err = io.StringIO(), io.StringIO()
        code = main(
            ["--db", db_path, *argv],
            stdin=_Stdin("" if stdin is None else stdin, tty=tty),
            stdout=out,
            stderr=err,
        )
        return Result(code, out.getvalue(), err.getvalue())

    return _run


# --------------------------------------------------------------------------- #
# Invocation, prompts and usage
# --------------------------------------------------------------------------- #


class TestUsage:
    def test_no_command_prints_help_to_stderr_and_exits_two(self, cli):
        result = cli()
        assert result.code == 2
        assert result.out == ""
        assert "usage: nimbus" in result.err

    def test_run_without_a_model_is_a_usage_error(self, cli):
        result = cli("run", "-p", "hi")
        assert result.code == 2
        assert "needs a model" in result.err

    def test_eval_needs_either_a_model_or_stored_runs(self, cli):
        result = cli("eval", "-p", "hi")
        assert result.code == 2
        assert "--model" in result.err and "--run" in result.err

    def test_a_prompt_is_read_from_a_pipe(self, cli):
        result = cli("run", "-m", "m1", stdin="  piped prompt  ")
        assert result.code == 0
        assert FAKE_PREFIX in result.out

    def test_a_dash_reads_stdin_explicitly(self, cli):
        result = cli("run", "-m", "m1", "-p", "-", stdin="from the dash")
        assert result.code == 0
        assert FAKE_PREFIX in result.out

    def test_a_piped_prompt_reaches_the_model_verbatim(self, cli, monkeypatch):
        """Indentation and trailing newlines are meaningful in piped content.

        Code, markdown and delimiter-based templates all carry whitespace that
        changes their meaning, and `--prompt` does not tidy its argument, so
        trimming here would make one prompt mean two things depending on how it
        arrived.
        """
        from nimbus.engine import runner
        from nimbus.engine.fake_model import FakeModel, Text

        seen = {}

        def capture(request, settings):
            seen["prompt"] = request.user_prompt
            return FakeModel([Text("ok")])

        monkeypatch.setattr(runner, "build_model", capture)
        piped = "  def f():\n      return 1\n"
        assert cli("run", "-m", "m1", stdin=piped).code == 0
        assert seen["prompt"] == piped

    def test_an_empty_pipe_is_a_usage_error_not_an_empty_run(self, cli):
        result = cli("run", "-m", "m1", stdin="   \n  ")
        assert result.code == 2
        assert "empty" in result.err

    def test_an_interactive_terminal_is_told_to_pass_a_prompt(self, cli):
        """Blocking on an invisible stdin read is the worst thing a CLI can do."""
        result = cli("run", "-m", "m1", tty=True)
        assert result.code == 2
        assert "a prompt is required" in result.err

    def test_a_system_prompt_can_come_from_a_file(self, cli, tmp_path):
        system = tmp_path / "system.txt"
        system.write_text("you are terse", encoding="utf-8")
        result = cli("run", "-m", "m1", "-p", "hi", "--system-file", str(system))
        assert result.code == 0

    def test_a_missing_system_file_is_a_usage_error_not_a_traceback(self, cli, tmp_path):
        result = cli("run", "-m", "m1", "-p", "hi", "--system-file", str(tmp_path / "gone.txt"))
        assert result.code == 2
        assert "--system-file" in result.err
        assert "Traceback" not in result.err

    def test_out_of_range_sampling_reads_as_a_usage_error(self, cli):
        result = cli("run", "-m", "m1", "-p", "hi", "--temperature", "5")
        assert result.code == 2
        assert "temperature" in result.err
        assert "Traceback" not in result.err

    @pytest.mark.parametrize(
        "argv",
        [("--json", "tools"), ("tools", "--json")],
        ids=["before the subcommand", "after the subcommand"],
    )
    def test_json_is_accepted_on_either_side_of_the_subcommand(self, cli, argv):
        """People type both, so both work."""
        assert cli(*argv).json()["toolsets"]


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


class TestRun:
    def test_the_answer_goes_to_stdout_and_the_progress_to_stderr(self, cli):
        result = cli("run", "-m", "m1", "-p", "hello there")
        assert result.code == 0
        assert result.out.startswith(FAKE_PREFIX)
        assert "|" not in result.out
        assert "| run " in result.err
        assert "| completed  run=" in result.err

    def test_stdout_ends_with_exactly_one_newline(self, cli):
        """So a shell prompt lands on its own line and a redirect is a clean file."""
        out = cli("run", "-m", "m1", "-p", "hi").out
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    @pytest.mark.parametrize(
        "script_text, expected",
        [
            ("no trailing newline", "no trailing newline\n"),
            ("already ends in one\n", "already ends in one\n"),
            ("ends in two\n\n", "ends in two\n\n"),
        ],
        ids=["adds one", "adds none", "adds none to a deliberate blank line"],
    )
    def test_the_contract_holds_whatever_the_model_ended_with(
        self, cli, monkeypatch, script_text, expected
    ):
        """Markdown and code answers routinely end in a newline of their own.

        Appending a second one unconditionally would put a blank line in every
        redirected file, which is exactly what the documented
        answer-plus-one-newline contract promises not to do.
        """
        from nimbus.engine import runner
        from nimbus.engine.fake_model import FakeModel, Text

        monkeypatch.setattr(
            runner, "build_model", lambda request, settings: FakeModel([Text(script_text)])
        )
        assert cli("run", "-m", "m1", "-p", "hi").out == expected

    def test_a_run_that_produces_no_text_writes_nothing_to_stdout(self, cli, monkeypatch):
        from nimbus.engine import runner
        from nimbus.engine.fake_model import FakeModel

        monkeypatch.setattr(runner, "build_model", lambda request, settings: FakeModel([]))
        assert cli("run", "-m", "m1", "-p", "hi").out == ""

    def test_json_puts_the_event_stream_on_stdout_and_nothing_on_stderr(self, cli):
        result = cli("run", "--json", "-m", "m1", "-p", "hi")
        assert result.code == 0
        assert result.err == ""
        events = result.ndjson()
        assert events[0]["type"] == "run_start"
        assert events[-1]["type"] == "run_complete"
        assert events[-1]["status"] == "completed"

    def test_the_run_is_persisted_and_readable_afterwards(self, cli):
        cli("run", "-m", "persisted-model", "-p", "remember me")
        listed = cli("runs", "--json").json()["items"]
        assert [row["model_id"] for row in listed] == ["persisted-model"]
        assert listed[0]["user_prompt"] == "remember me"

    def test_an_unknown_toolset_fails_before_anything_is_executed(self, cli):
        result = cli("run", "-m", "m1", "-p", "hi", "--toolset", "nope")
        assert result.code == 1
        assert "Unknown toolset" in result.err
        assert cli("runs", "--json").json()["items"] == []

    def test_a_guardrail_on_a_non_bedrock_provider_is_refused(self, cli):
        result = cli(
            "run", "-m", "m1", "-p", "hi", "--provider", "openai", "--guardrail-id", "g1"
        )
        assert result.code == 1
        assert "Guardrails are a Bedrock feature" in result.err

    def test_a_model_failure_mid_stream_exits_one_and_persists_the_row(self, cli, monkeypatch):
        """A failure *during* a run reports in-band; the row still settles as error.

        Only the model is scripted here -- the real engine, store and renderer
        run, which is what makes the exit code and the persisted status mean
        something.
        """
        from strands.types.exceptions import ModelThrottledException

        from nimbus.engine import runner
        from nimbus.engine.fake_model import Error, FakeModel, Text

        script = [Text("partial answer"), Error(ModelThrottledException("slow down"))]
        monkeypatch.setattr(runner, "build_model", lambda request, settings: FakeModel(script))

        result = cli("run", "-m", "m1", "-p", "hi")
        assert result.code == 1
        assert result.out == "partial answer\n"
        assert "| error [model_throttled] slow down  (retryable)" in result.err

        stored = cli("runs", "--json").json()["items"][0]
        assert stored["status"] == "error"
        assert stored["output"] == "partial answer"
        assert stored["error"]["code"] == "model_throttled"

    def test_a_registered_toolset_is_accepted(self, cli):
        assert cli("run", "-m", "m1", "-p", "hi", "--toolset", "fraud-detection").code == 0

    def test_an_unknown_mcp_server_fails_before_anything_is_executed(self, cli):
        result = cli("run", "-m", "m1", "-p", "hi", "--mcp-server", "a", "--mcp-server", "b")
        assert result.code == 1
        assert "MCP server 'a' not found" in result.err
        assert cli("runs", "--json").json()["items"] == []

    def test_saved_mcp_servers_are_named_on_the_run(self, cli, db_path):
        from nimbus.mcp.store import SqliteMcpStore
        from nimbus.store import db

        db.init_db(db_path)
        saved = SqliteMcpStore().create(name="s", url="http://127.0.0.1:9/mcp", headers={})
        result = cli("run", "-m", "m1", "-p", "hi", "--mcp-server", saved.id)
        assert result.code == 1  # nothing listens there: reported in-band
        assert "mcp_connection_failed" in result.err
        stored = cli("runs", "--json").json()["items"][0]
        assert stored["config"]["mcp_servers"] == [saved.id]


# --------------------------------------------------------------------------- #
# eval
# --------------------------------------------------------------------------- #


class TestEval:
    def test_a_determinism_experiment_prints_its_result_and_grade(self, cli):
        result = cli("eval", "-m", "m1", "-p", "same prompt", "-n", "2")
        assert result.code == 0
        terminal = result.json()
        assert terminal["status"] == "completed"
        assert len(terminal["run_ids"]) == 2
        assert "| grade " in result.err

    def test_json_streams_one_progress_event_per_line(self, cli):
        result = cli("eval", "--json", "-m", "m1", "-p", "hi", "-n", "2")
        assert result.err == ""
        types = [event["type"] for event in result.ndjson()]
        assert types[0] == "eval_start"
        assert types[-1] == "eval_complete"

    def test_json_lines_are_the_same_bytes_the_events_endpoint_serves(self, cli):
        """One wire format, whichever door you came through."""
        from nimbus.evals.jobs import to_json_line

        lines = cli("eval", "--json", "-m", "m1", "-p", "hi", "-n", "2").out.splitlines(
            keepends=True
        )
        assert lines == [to_json_line(json.loads(line)) for line in lines]

    def test_stored_runs_can_be_graded_without_executing_anything_new(self, cli):
        cli("run", "-m", "m1", "-p", "one")
        cli("run", "-m", "m1", "-p", "two")
        run_ids = [row["id"] for row in cli("runs", "--json").json()["items"]]

        result = cli("eval", "--run", run_ids[0], "--run", run_ids[1])
        assert result.code == 0
        assert result.json()["status"] == "completed"
        # Still two runs: grading executed nothing.
        assert len(cli("runs", "--json").json()["items"]) == 2

    def test_a_non_bedrock_judge_must_name_its_own_model(self, cli):
        """The default judge model is a Bedrock id, so inheriting it elsewhere

        builds a judge that can only fail at the provider. No default is
        invented for the other providers -- the right one is the caller's to
        name.
        """
        result = cli("eval", "-m", "m1", "-p", "hi", "--grader-provider", "openai")
        assert result.code == 2
        assert "--grader-provider openai needs an explicit --grader-model" in result.err
        assert "amazon.nova-pro-v1:0" in result.err

    def test_a_non_bedrock_judge_with_an_explicit_model_is_accepted(self, cli):
        result = cli(
            "eval",
            "-m",
            "m1",
            "-p",
            "hi",
            "-n",
            "2",
            "--grader-provider",
            "openai",
            "--grader-model",
            "gpt-4o",
        )
        assert result.code == 0

    def test_a_bedrock_judge_still_needs_no_model(self, cli):
        assert cli("eval", "-m", "m1", "-p", "hi", "-n", "2").code == 0

    def test_an_unknown_run_id_fails_before_the_job_starts(self, cli):
        result = cli("eval", "--run", "nope")
        assert result.code == 1
        assert "not found" in result.err.lower()


# --------------------------------------------------------------------------- #
# Discovery and history
# --------------------------------------------------------------------------- #


class TestTools:
    def test_the_table_names_each_toolset_and_its_tools(self, cli):
        result = cli("tools")
        assert result.code == 0
        assert result.lines[0].startswith("TOOLSET")
        assert any("fraud-detection" in line for line in result.lines)

    def test_json_matches_the_get_tools_payload(self, cli):
        names = [entry["name"] for entry in cli("tools", "--json").json()["toolsets"]]
        assert "fraud-detection" in names

    def test_an_empty_registry_says_so_on_stderr(self, cli, monkeypatch):
        monkeypatch.setattr(commands, "list_handlers", dict)
        result = cli("tools")
        assert result.out == ""
        assert "no toolsets registered" in result.err


class TestModels:
    @staticmethod
    def _catalog(monkeypatch, result: CatalogResult):
        class StubCatalog:
            async def collect(self, settings, bedrock_catalog):
                return result

        monkeypatch.setattr(commands, "ProviderCatalog", StubCatalog)

    def test_models_are_listed_with_the_provider_to_send_back(self, cli, monkeypatch):
        self._catalog(
            monkeypatch,
            CatalogResult(
                models=[{"model_id": "m1", "name": "Model One", "source": "bedrock"}],
                providers={"bedrock": {"configured": True}},
                cached=False,
            ),
        )
        result = cli("models")
        assert result.code == 0
        assert result.lines[0].split() == ["PROVIDER", "MODEL", "ID", "NAME"]
        assert "bedrock" in result.lines[1] and "Model One" in result.lines[1]

    def test_an_empty_catalog_explains_itself_rather_than_printing_a_bare_header(
        self, cli, monkeypatch
    ):
        self._catalog(monkeypatch, CatalogResult(models=[], providers={}, cached=True))
        result = cli("models")
        assert result.out == ""
        assert "no models available" in result.err
        assert "not configured:" in result.err

    def test_json_carries_the_providers_block_too(self, cli, monkeypatch):
        self._catalog(
            monkeypatch,
            CatalogResult(models=[], providers={"ollama": {"configured": False}}, cached=True),
        )
        payload = cli("models", "--json").json()
        assert payload == {
            "models": [],
            "providers": {"ollama": {"configured": False}},
            "cached": True,
        }


class TestRunsAndShow:
    def test_an_empty_store_says_so_instead_of_printing_a_header(self, cli):
        result = cli("runs")
        assert result.code == 0
        assert result.out == ""
        assert "no runs stored" in result.err

    def test_runs_are_listed_newest_first_with_their_status(self, cli):
        cli("run", "-m", "older", "-p", "1")
        cli("run", "-m", "newer", "-p", "2")
        listed = cli("runs").out
        assert listed.index("newer") < listed.index("older")
        assert "completed" in listed

    def test_a_model_filter_narrows_the_listing(self, cli):
        cli("run", "-m", "keep", "-p", "1")
        cli("run", "-m", "drop", "-p", "2")
        rows = cli("runs", "--json", "--model", "keep").json()["items"]
        assert [row["model_id"] for row in rows] == ["keep"]

    def test_a_full_page_offers_the_cursor_to_continue_from(self, cli):
        cli("run", "-m", "m1", "-p", "1")
        cli("run", "-m", "m1", "-p", "2")
        result = cli("runs", "--limit", "1")
        assert "more: --cursor " in result.err
        assert len(result.lines) == 2  # header + one row

    def test_show_prints_a_run_as_json(self, cli):
        cli("run", "-m", "m1", "-p", "show me")
        run_id = cli("runs", "--json").json()["items"][0]["id"]
        detail = cli("show", run_id).json()
        assert detail["id"] == run_id
        assert detail["user_prompt"] == "show me"

    def test_show_falls_through_to_evaluations(self, cli):
        cli("eval", "-m", "m1", "-p", "hi", "-n", "2")
        evaluation_id = cli("eval", "-m", "m1", "-p", "hi", "-n", "2").json()["evaluation_id"]
        detail = cli("show", evaluation_id).json()
        assert detail["id"] == evaluation_id
        assert detail["kind"] == "determinism"

    def test_an_id_that_is_neither_names_both_possibilities(self, cli):
        result = cli("show", "nope")
        assert result.code == 1
        assert result.err == "nimbus: No run or evaluation with id 'nope'\n"


# --------------------------------------------------------------------------- #
# serve, and the top-level failure paths
# --------------------------------------------------------------------------- #


def test_serve_is_not_a_coroutine():
    """`serve` must stay synchronous.

    uvicorn.run() calls asyncio.run() itself and, with --reload, forks a
    supervisor. If this were a coroutine the dispatcher would drive it inside
    a running loop and `nimbus serve` would die with "asyncio.run()
    cannot be called from a running event loop" instead of serving.
    """
    assert not inspect.iscoroutinefunction(commands.serve)


def test_serve_starts_uvicorn_outside_any_running_event_loop(cli, monkeypatch):
    """The regression guard for the above, asserted where it actually breaks.

    An earlier version of this test stubbed uvicorn with a no-op and checked
    only the arguments, which is exactly why it passed while the command was
    unusable. The stub now asserts what uvicorn itself requires: that no loop
    is already running when it is called.
    """
    calls = {}

    def fake_run(target, **kwargs):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls["target"] = target
        calls.update(kwargs)

    monkeypatch.setitem(
        sys.modules, "uvicorn", types.SimpleNamespace(run=fake_run)
    )
    result = cli("serve", "--host", "0.0.0.0", "--port", "9999")

    assert result.code == 0
    assert calls["target"] == "nimbus.main:app"
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 9999
    assert calls["reload"] is False
    assert "http://0.0.0.0:9999" in result.err


def test_serve_exports_the_db_override_so_the_served_app_sees_it(cli, monkeypatch, db_path):
    """uvicorn imports the app by string -- and with --reload, in a child.

    The app's lifespan builds its own Settings from the environment, so an
    override held only in our Settings object would leave the CLI and the
    server it just started reading different history stores.
    """
    monkeypatch.delenv("NIMBUS_DB_PATH", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None))
    assert cli("serve").code == 0
    assert os.environ["NIMBUS_DB_PATH"] == db_path


def test_db_pins_the_backend_to_sqlite_not_just_the_path(cli, monkeypatch, db_path):
    """--db is an isolated scratch store, so it has to beat the environment.

    Exporting only the path left `get_history_repo` choosing its backend from
    `history_backend`, so an environment carrying dynamodb created the file and
    then wrote every run to DynamoDB anyway.
    """
    from nimbus.config import Settings
    from nimbus.store.repo import SqliteHistoryRepo, get_history_repo

    monkeypatch.setenv("NIMBUS_HISTORY_BACKEND", "dynamodb")
    monkeypatch.setenv("NIMBUS_EVAL_TABLE", "some-table")

    assert cli("runs").code == 0
    assert isinstance(get_history_repo(Settings()), SqliteHistoryRepo)
    assert os.environ["NIMBUS_HISTORY_BACKEND"] == "sqlite"


def test_a_malformed_setting_is_a_diagnostic_not_a_traceback(cli, monkeypatch):
    """Settings() parses the whole NIMBUS_ environment and raises on a bad value.

    Built outside the guarded block it took down even `tools`, which never
    touches the setting, with a raw pydantic traceback.

    `local_evals` rather than `history_backend` because --db (which the fixture
    always passes) deliberately overrides the latter, so a bad value there
    never reaches validation.
    """
    monkeypatch.setenv("NIMBUS_LOCAL_EVALS", "maybe")
    result = cli("tools")
    assert result.code == 2
    assert result.err.startswith("nimbus: local_evals: ")
    assert "Traceback" not in result.err


def test_ctrl_c_reports_cancellation_with_the_shell_s_own_code(cli, monkeypatch):
    async def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setitem(COMMANDS, "tools", interrupted)
    result = cli("tools")
    assert result.code == 130
    assert "cancelled" in result.err


def test_an_application_error_is_one_line_not_a_traceback(cli, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise NotFoundError("nothing here")

    monkeypatch.setitem(COMMANDS, "tools", boom)
    result = cli("tools")
    assert result.code == 1
    assert result.err == "nimbus: nothing here\n"
    assert isinstance(NotFoundError("x"), AppError)


def test_the_default_database_is_used_when_no_db_is_passed(monkeypatch, tmp_path):
    """--db is an override; without it the store follows the environment."""
    monkeypatch.setenv("NIMBUS_FAKE_MODEL", "1")
    monkeypatch.setenv("NIMBUS_DB_PATH", str(tmp_path / "from-env.db"))
    monkeypatch.setattr(store_db, "_engine", None)
    store_repo.reset_cache()
    out, err = io.StringIO(), io.StringIO()
    assert main(["run", "-m", "m1", "-p", "hi"], stdin=_Stdin(), stdout=out, stderr=err) == 0
    assert (tmp_path / "from-env.db").exists()


def test_the_module_entry_point_exposes_the_same_main():
    """`python -m nimbus.cli` and the console script are one entry point."""
    from nimbus.cli import __main__

    assert __main__.main is main


# --------------------------------------------------------------------------- #
# eval --suite
# --------------------------------------------------------------------------- #

SUITE_YAML = """\
name: support
run_config:
  model_id: fake.model
  system_prompt: You are a support agent.
  inference:
    temperature: 0.2
cases:
  - id: refund-window
    input: Can I return shoes after 45 days?
    expected: No. Returns are accepted within 30 days.
    criteria: Must state the 30-day window.
  - id: store-hours
    input: When do you open on Sunday?
"""


@pytest.fixture
def suite_file(tmp_path):
    def write(text: str = SUITE_YAML, name: str = "suite.yaml"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    return write


@pytest.fixture
def captured_runs(monkeypatch):
    """The RunRequest of every run the CLI executes."""
    seen: list = []

    def capture(request, settings):
        seen.append(request)
        return FakeModel([Text("We accept returns within 30 days.")])

    # `eval` builds its models through commands.build_model (`run` goes through
    # runner.build_model instead), so that is the seam to watch.
    monkeypatch.setattr(commands, "build_model", capture)
    return seen


class TestSuite:
    def test_a_suite_file_runs_every_case_and_reports_each(self, cli, suite_file):
        result = cli("eval", "--suite", suite_file())

        assert result.code == 0, result.err
        payload = json.loads(result.out)
        assert payload["status"] == "completed"
        assert [case["id"] for case in payload["result"]["cases"]] == [
            "refund-window",
            "store-hours",
        ]
        assert "2/2 cases passed" in result.err
        assert re.search(r"PASS\s+0\.95\s+refund-window", result.err)
        assert "run 0 [refund-window] started" in result.err

    def test_json_suite_files_work_too(self, cli, suite_file):
        text = json.dumps(
            {
                "run_config": {"model_id": "fake.model"},
                "cases": [{"id": "only", "input": "hi?"}],
            }
        )

        result = cli("eval", "--suite", suite_file(text, "suite.json"))

        assert result.code == 0, result.err
        assert json.loads(result.out)["result"]["cases"][0]["id"] == "only"

    def test_each_case_is_run_with_the_files_config_and_its_own_input(
        self, cli, suite_file, captured_runs
    ):
        result = cli("eval", "--suite", suite_file())

        assert result.code == 0, result.err
        assert sorted(run.user_prompt for run in captured_runs) == [
            "Can I return shoes after 45 days?",
            "When do you open on Sunday?",
        ]
        assert {run.system_prompt for run in captured_runs} == {"You are a support agent."}
        assert {run.inference.temperature for run in captured_runs} == {0.2}

    def test_flags_given_override_the_file_and_only_those(self, cli, suite_file, captured_runs):
        """`-m other` is how you run the same suite on another model."""
        text = SUITE_YAML.replace(
            "model_id: fake.model", "model_id: fake.model\n  provider: openai"
        )

        result = cli(
            "eval", "--suite", suite_file(text), "-m", "other.model", "--max-tokens", "50"
        )

        assert result.code == 0, result.err
        run = captured_runs[0]
        assert run.model_id == "other.model"  # overridden
        assert run.inference.max_tokens == 50  # added
        assert run.inference.temperature == 0.2  # kept: inference merges, not replaces
        # Not passed, so the file's choice stands -- even though --provider has
        # a default. This is why the run options default to None.
        assert run.provider == "openai"

    def test_the_files_rubric_is_used_and_rubric_overrides_it(self, cli, suite_file, monkeypatch):
        from nimbus.evals import engine as evals_engine

        seen: list = []
        real = evals_engine.execute_evaluation_with_seam

        async def spy(request, *args, **kwargs):
            seen.append(request.rubric)
            return await real(request, *args, **kwargs)

        monkeypatch.setattr(commands, "execute_evaluation_with_seam", spy)
        path = suite_file(SUITE_YAML + "rubric: From the file.\n")

        assert cli("eval", "--suite", path).code == 0
        assert cli("eval", "--suite", path, "--rubric", "From the flag.").code == 0
        assert seen == ["From the file.", "From the flag."]

    def test_json_mode_streams_events_that_name_their_case(self, cli, suite_file):
        result = cli("--json", "eval", "--suite", suite_file())

        assert result.code == 0, result.err
        events = [json.loads(line) for line in result.out.splitlines()]
        runs = [e for e in events if e["type"] == "run_completed"]
        assert {e["case_id"] for e in runs} == {"refund-window", "store-hours"}

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            (["--run", "abc"], "--suite and --run are different evaluations"),
            (["-p", "hello"], "each case's `input` is the prompt"),
            (["-n", "5"], "set in the suite file"),
        ],
    )
    def test_flags_that_cannot_apply_are_rejected_not_ignored(
        self, cli, suite_file, extra, message
    ):
        result = cli("eval", "--suite", suite_file(), *extra)

        assert result.code == 2
        assert message in result.err
        assert result.out == ""

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("cases: [unclosed", "not valid YAML or JSON at line 1"),
            ("- just\n- a list\n", "expected a mapping at the top level"),
            (
                "run_config: {model_id: m}\ncases:\n  - id: a\n",
                "cases.0.input: Field required",
            ),
            (
                "run_config: {model_id: m}\n"
                "cases:\n  - {id: a, input: x}\n  - {id: a, input: y}\n",
                "Case ids must be unique within a suite; repeated: a",
            ),
            # No run_config means no model: the message names the field that matters.
            ("cases:\n  - {id: a, input: x}\n", "run_config.model_id: Field required"),
            (
                "run_config: {model_id: m, user_prompt: hi}\ncases:\n  - {id: a, input: x}\n",
                "each case's `input` is the prompt",
            ),
        ],
    )
    def test_a_bad_suite_file_is_one_line_naming_the_file_and_the_field(
        self, cli, suite_file, text, message
    ):
        path = suite_file(text)

        result = cli("eval", "--suite", path)

        assert result.code == 2
        assert result.err.count("\n") == 1, result.err
        assert result.err.startswith(f"nimbus: --suite {path}")
        assert message in result.err

    def test_a_missing_suite_file_is_a_usage_error(self, cli, tmp_path):
        result = cli("eval", "--suite", str(tmp_path / "nope.yaml"))

        assert result.code == 2
        assert "--suite: No such file or directory" in result.err

    def test_failed_assertions_are_shown_per_case(self, cli, suite_file):
        text = """\
run_config: {model_id: fake.model}
repeats: 2
cases:
  - id: echoes
    input: hi
    judge: false
    assert:
      - contains: "[fake-model]"
      - regex: "for fake\\\\.model\\\\.$"
  - id: wrong
    input: hi
    assert:
      - contains: refund
      - max_tool_calls: 0
"""
        result = cli("eval", "--suite", suite_file(text))

        assert result.code == 0, result.err
        cases = {case["id"]: case for case in json.loads(result.out)["result"]["cases"]}
        assert cases["echoes"]["status"] == "passed"
        assert cases["echoes"]["judged"] is False
        assert cases["wrong"]["status"] == "failed"
        assert cases["wrong"]["repeats"][1]["assertions"][0] == {
            "type": "contains",
            "passed": False,
            "detail": 'output does not contain "refund"',
        }
        assert re.search(r"PASS\s+1\.00\s+echoes", result.err)
        assert re.search(r"FAIL\s+0\.95\s+wrong  2 of 4 assertion checks failed", result.err)
        assert 'x contains (repeats 1, 2 of 2): output does not contain "refund"' in result.err

    def test_a_bad_assertion_is_a_usage_error_before_anything_runs(self, cli, suite_file):
        text = "run_config: {model_id: m}\ncases:\n  - {id: a, input: x, assert: [{regex: '('}]}\n"
        path = suite_file(text)

        result = cli("eval", "--suite", path)

        assert result.code == 2
        assert result.err.count("\n") == 1, result.err
        assert "cases.0.assert.0.regex: Value error, invalid regex '('" in result.err


class TestSuiteOverrides:
    """Every override path, down to the request the engine receives."""

    def _request(self, suite_file, *argv):
        import argparse  # noqa: F401 - mirrors how main builds the namespace

        from nimbus.cli.main import build_parser, prepare

        args = build_parser().parse_args(["eval", "--suite", suite_file(), *argv])
        prepare(args, _Stdin("", tty=True))
        return commands.build_eval_request(args)

    def test_grader_flags_override_the_files_grader(self, suite_file):
        text = SUITE_YAML + "grader:\n  model_id: file.judge\n  system_prompt: from the file\n"
        path_writer = lambda: suite_file(text)  # noqa: E731

        request = self._request(
            path_writer, "--grader-provider", "openai", "--grader-model", "gpt-judge"
        )

        assert request.grader.provider == "openai"
        assert request.grader.model_id == "gpt-judge"
        assert request.grader.system_prompt == "from the file"  # not passed, so kept

    def test_a_guardrail_flag_applies_to_every_case(self, suite_file):
        request = self._request(suite_file, "--guardrail-id", "g-1", "--guardrail-version", "3")

        run = request.suite.run_config.for_case(request.suite.cases[0])
        assert (run.guardrail.id, run.guardrail.version) == ("g-1", "3")

    def test_a_system_file_overrides_the_files_system_prompt(self, suite_file, tmp_path):
        system = tmp_path / "system.txt"
        system.write_text("From a file of its own.", encoding="utf-8")

        request = self._request(suite_file, "--system-file", str(system))

        assert request.suite.run_config.system_prompt == "From a file of its own."

    def test_a_non_bedrock_file_judge_without_a_model_is_a_usage_error(self, cli, suite_file):
        result = cli("eval", "--suite", suite_file(SUITE_YAML + "grader:\n  provider: openai\n"))

        assert result.code == 2
        assert "needs an explicit grader.model_id" in result.err

    def test_grader_provider_alone_uses_the_files_judge_model(self, suite_file):
        text = SUITE_YAML + "grader:\n  model_id: gpt-judge\n"

        request = self._request(lambda: suite_file(text), "--grader-provider", "openai")

        assert (request.grader.provider, request.grader.model_id) == ("openai", "gpt-judge")

    def test_calibrate_turns_on_the_suites_calibration(self, suite_file):
        assert self._request(suite_file).suite.calibrate is False
        assert self._request(suite_file, "--calibrate").suite.calibrate is True

    def test_calibrate_without_a_suite_is_a_usage_error(self, cli):
        result = cli("eval", "-m", "m1", "-p", "hi", "--calibrate")

        assert result.code == 2
        assert "--calibrate" in result.err
        assert "--suite" in result.err

    def test_grader_provider_alone_without_any_judge_model_is_a_usage_error(self, cli, suite_file):
        result = cli("eval", "--suite", suite_file(), "--grader-provider", "openai")

        assert result.code == 2
        assert "--suite" in result.err
        assert "needs an explicit grader.model_id" in result.err


@pytest.mark.parametrize(
    "argv",
    [
        ["eval", "-m", "m1", "-p", "hi"],
        ["eval", "--run", "run-1"],
    ],
)
def test_every_cli_evaluation_says_it_came_from_the_cli(argv):
    from nimbus.cli.main import build_parser, prepare

    args = build_parser().parse_args(argv)
    prepare(args, _Stdin("", tty=True))

    assert commands.build_eval_request(args).source == "cli"


def test_a_cli_suite_says_it_came_from_the_cli(suite_file):
    from nimbus.cli.main import build_parser, prepare

    args = build_parser().parse_args(["eval", "--suite", suite_file()])
    prepare(args, _Stdin("", tty=True))

    assert commands.build_eval_request(args).source == "cli"


def test_the_documented_example_suite_is_valid(cli):
    """docs/examples/support-suite.yaml is what people will copy; it must work."""
    example = next(
        parent / "docs" / "examples" / "support-suite.yaml"
        for parent in __import__("pathlib").Path(__file__).resolve().parents
        if (parent / "docs" / "examples" / "support-suite.yaml").is_file()
    )

    result = cli("eval", "--suite", str(example), "-m", "fake.model")

    assert result.code == 0, result.err
    cases = json.loads(result.out)["result"]["cases"]
    assert [case["id"] for case in cases] == [
        "refund-window",
        "sunday-hours",
        "late-return",
        "off-topic",
    ]


# --------------------------------------------------------------------------- #
# eval: CI gating (--fail-under, --fail-on-case-failure, --gate, --junit)
# --------------------------------------------------------------------------- #

#: The fake judge scores every case 0.95, so the headline score is 95 and a
#: case passes at the default threshold. 0.99 makes every case fail.
FAILING_SUITE_YAML = SUITE_YAML + "pass_threshold: 0.99\n"


class TestGating:
    @pytest.mark.parametrize(
        ("suite", "flags", "code"),
        [
            # Every case passes; score 95.
            (SUITE_YAML, [], 0),
            (SUITE_YAML, ["--fail-under", "95"], 0),
            (SUITE_YAML, ["--fail-under", "95.5"], 3),
            (SUITE_YAML, ["--fail-on-case-failure"], 0),
            (SUITE_YAML, ["--gate"], 0),
            (SUITE_YAML, ["--gate", "--fail-on-case-failure", "--fail-under", "90"], 0),
            (SUITE_YAML, ["--gate", "--fail-on-case-failure", "--fail-under", "96"], 3),
            # Every case fails its threshold; the score is still 95.
            (FAILING_SUITE_YAML, [], 0),
            (FAILING_SUITE_YAML, ["--fail-under", "90"], 0),
            (FAILING_SUITE_YAML, ["--fail-on-case-failure"], 3),
            (FAILING_SUITE_YAML, ["--gate"], 3),
            (FAILING_SUITE_YAML + "min_pass_rate: 0.5\n", ["--gate"], 3),
            (FAILING_SUITE_YAML + "min_pass_rate: 0\n", ["--gate"], 0),
            (FAILING_SUITE_YAML + "min_pass_rate: 0\n", ["--gate", "--fail-on-case-failure"], 3),
            # min_pass_rate is inert without --gate.
            (FAILING_SUITE_YAML + "min_pass_rate: 1\n", [], 0),
        ],
    )
    def test_suite_exit_codes(self, cli, suite_file, suite, flags, code):
        result = cli("eval", "--suite", suite_file(suite), *flags)

        assert result.code == code, result.err
        # The result is printed either way: a gate changes the exit code only.
        assert json.loads(result.out)["status"] == "completed"
        if code == 3:
            assert "| gate failed: " in result.err
        elif flags:
            assert "| gate passed" in result.err
        else:
            assert "gate" not in result.err

    def test_json_mode_is_gated_too(self, cli, suite_file):
        result = cli("--json", "eval", "--suite", suite_file(FAILING_SUITE_YAML), "--gate")

        assert result.code == 3
        assert json.loads(result.out.splitlines()[-1])["type"] == "eval_complete"
        assert "| gate failed: pass rate 0% is under the suite's bar of 100%" in result.err

    @pytest.mark.parametrize(("bar", "code"), [("95", 0), ("96", 3)])
    def test_fail_under_gates_a_determinism_experiment(self, cli, bar, code):
        result = cli("eval", "-m", "m1", "-p", "hi", "-n", "2", "--fail-under", bar)

        assert result.code == code, result.err

    @pytest.mark.parametrize(
        ("status", "code"), [("error", 1), ("cancelled", 130), ("completed", 3)]
    )
    @pytest.mark.parametrize(
        "flags", [["--fail-under", "50"], ["--fail-on-case-failure"], ["--gate"]]
    )
    def test_the_harnesss_own_verdict_comes_before_the_gate(
        self, cli, suite_file, monkeypatch, tmp_path, status, code, flags
    ):
        """An evaluation that errored has no result to gate: it is 1, not 3."""

        async def settled(request, emit, store, **kwargs):
            result = None
            if status == "completed":
                result = {
                    "score": 10,
                    "metrics": {"pass_rate": 0.0},
                    "cases": [{"id": "refund-window", "passed": False, "status": "failed"}],
                }
            return {"evaluation_id": "ev", "status": status, "result": result, "error": None}

        monkeypatch.setattr(commands, "execute_evaluation_with_seam", settled)
        report = tmp_path / "report.xml"

        result = cli("eval", "--suite", suite_file(), *flags, "--junit", str(report))

        assert result.code == code
        # The report is written whatever happened, so CI shows it.
        assert report.is_file()

    @pytest.mark.parametrize(
        ("flags", "message"),
        [
            (["--gate"], "--gate judges the cases of a suite"),
            (["--fail-on-case-failure"], "--fail-on-case-failure judges the cases of a suite"),
            (["--junit", "r.xml"], "--junit judges the cases of a suite"),
        ],
    )
    def test_suite_only_flags_need_a_suite(self, cli, flags, message):
        result = cli("eval", "-m", "m1", "-p", "hi", *flags)

        assert result.code == 2
        assert message in result.err

    @pytest.mark.parametrize("bar", ["101", "-1", "abc", "nan"])
    def test_fail_under_is_a_score_from_0_to_100(self, cli, bar, capsys):
        with pytest.raises(SystemExit) as exited:
            cli("eval", "-m", "m1", "-p", "hi", "--fail-under", bar)

        assert exited.value.code == 2
        assert "--fail-under" in capsys.readouterr().err

    def test_min_pass_rate_is_a_fraction(self, cli, suite_file):
        result = cli("eval", "--suite", suite_file(SUITE_YAML + "min_pass_rate: 2\n"), "--gate")

        assert result.code == 2
        assert "min_pass_rate" in result.err

    def test_junit_report_for_a_suite(self, cli, suite_file, tmp_path):
        from xml.etree import ElementTree as ET

        report = tmp_path / "junit.xml"

        result = cli(
            "eval", "--suite", suite_file(FAILING_SUITE_YAML), "--junit", str(report)
        )

        assert result.code == 0  # a report is not a gate
        assert f"| JUnit report written to {report}" in result.err
        root = ET.parse(report).getroot()
        [suite] = root.findall("testsuite")
        assert suite.get("name") == "support"
        assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == ("2", "2", "0")
        cases = suite.findall("testcase")
        assert [case.get("name") for case in cases] == ["refund-window", "store-hours"]
        for case in cases:
            assert set(case.attrib) == {"name", "classname", "time"}
            assert case.find("failure").get("message") == f"case {case.get('name')} failed"
            assert case.find("failure").text.startswith("score 0.95")
        evaluation_id = json.loads(result.out)["evaluation_id"]
        assert suite.find("properties/property").get("value") == evaluation_id

    def test_a_junit_path_that_cannot_be_written_is_a_harness_failure(
        self, cli, suite_file, tmp_path
    ):
        report = tmp_path / "missing-dir" / "junit.xml"

        result = cli("eval", "--suite", suite_file(), "--junit", str(report))

        assert result.code == 1
        assert f"nimbus: --junit {report}: No such file or directory" in result.err


# --------------------------------------------------------------------------- #
# mcp
# --------------------------------------------------------------------------- #


class TestMcp:
    def test_add_list_update_remove_locally(self, cli, monkeypatch):
        import getpass

        prompts = []
        monkeypatch.setattr(
            getpass, "getpass", lambda prompt, stream=None: prompts.append(prompt) or "typed"
        )
        added = cli(
            "mcp", "add", "GitHub", "http://localhost:9/mcp",
            "--header", "Authorization: Bearer inline", "--header", "X-Team",
        )
        assert added.code == 0, added.err
        server_id = added.out.strip()
        assert prompts == ["X-Team: "]
        assert "Bearer inline" not in added.out + added.err and "typed" not in added.err

        from nimbus.mcp.store import SqliteMcpStore

        assert SqliteMcpStore().get(server_id).headers == {
            "Authorization": "Bearer inline",
            "X-Team": "typed",
        }

        table = cli("mcp", "list")
        assert table.out.splitlines()[0].split() == ["ID", "NAME", "URL", "HEADERS"]
        assert server_id in table.out and "Authorization, X-Team" in table.out

        updated = cli(
            "mcp", "update", server_id, "--url", "http://localhost:10/mcp",
            "--header", "X-New: 1", "--remove-header", "X-Team",
        )
        assert updated.code == 0, updated.err
        assert SqliteMcpStore().get(server_id).headers == {
            "Authorization": "Bearer inline",
            "X-New": "1",
        }

        assert cli("mcp", "remove", server_id).code == 0
        empty = cli("mcp", "list")
        assert empty.out == "" and "no MCP servers saved" in empty.err
        assert cli("mcp", "list", "--json").json() == {"servers": []}

    def test_test_lists_tools_or_fails(self, cli):
        from tests.mcp_http_server import API_KEY, running_mcp_server

        with running_mcp_server() as url:
            good = cli("mcp", "add", "T", url, "--header", f"X-Api-Key: {API_KEY}").out.strip()
            bad = cli("mcp", "add", "T2", url).out.strip()
            ok = cli("mcp", "test", good)
            as_json = cli("mcp", "test", good, "--json").json()
            refused = cli("mcp", "test", bad)
        assert ok.code == 0 and "connected: 3 tool(s)" in ok.err
        assert ok.out.splitlines()[0].split() == ["TOOL", "DESCRIPTION"]
        assert "Echo the text back, reversed." in ok.out
        assert as_json["ok"] is True
        assert refused.code == 1 and "401" in refused.err

    def test_bad_invocations(self, cli):
        assert cli("mcp", "add", "x", "ftp://nope").code == 1
        assert cli("mcp", "add", "x", "http://localhost/mcp", "--header", ": v").code == 1
        saved = cli("mcp", "add", "x", "http://localhost/mcp", "--json").json()
        nothing = cli("mcp", "update", saved["id"])
        assert nothing.code == 1 and "nothing to change" in nothing.err
        both = cli("mcp", "update", saved["id"], "--header", "A: 1", "--remove-header", "A")
        assert both.code == 1 and "both set and removed" in both.err
        assert cli("mcp", "test", "missing").code == 1


# --------------------------------------------------------------------------- #
# eval --suite --arm: one suite, several models
# --------------------------------------------------------------------------- #

ASSERTING_SUITE_YAML = """\
run_config:
  model_id: fake.model
cases:
  - id: refund-window
    input: Can I return shoes after 45 days?
    judge: false
    assert:
      - contains: 30 days
  - id: store-hours
    input: When do you open on Sunday?
    judge: false
    assert:
      - contains: Sunday
"""

#: What each model in the arm tests answers: only `good` satisfies both cases.
ARM_ANSWERS = {
    "good": "Returns within 30 days. We open Sunday at 10.",
    "half": "Returns within 30 days.",
    "bad": "No idea.",
}


@pytest.fixture
def answering_arms(monkeypatch):
    """Every arm's model, and the RunRequests the CLI executed, in order."""
    seen: list = []

    def build(request, settings):
        seen.append(request)
        return FakeModel([Text(ARM_ANSWERS[request.model_id])])

    monkeypatch.setattr(commands, "build_model", build)
    return seen


class TestArms:
    def test_the_suite_runs_once_per_model_and_the_models_are_compared(
        self, cli, suite_file, answering_arms
    ):
        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML),
            "--arm", "bad", "--arm", "good", "--arm", "half",
        )  # fmt: skip

        assert result.code == 0, result.err
        comparison = result.json()
        assert comparison["ranking"] == [
            "bedrock:good",
            "bedrock:half",
            "bedrock:bad",
        ]
        assert comparison["winner"] == "bedrock:good"
        assert comparison["split_cases"] == ["refund-window", "store-hours"]
        # Six runs: two cases on each of three models, model by model, in order.
        assert [run.model_id for run in answering_arms] == ["bad"] * 2 + ["good"] * 2 + ["half"] * 2

    def test_each_arm_is_its_own_stored_evaluation(self, cli, suite_file, answering_arms):
        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML), "--arm", "good", "--arm", "bad"
        )

        assert result.code == 0, result.err
        ids = [arm["evaluation_id"] for arm in result.json()["arms"]]
        assert len(set(ids)) == 2
        for evaluation_id in ids:
            shown = cli("show", evaluation_id)
            assert shown.code == 0, shown.err
            assert shown.json()["status"] == "completed"

    def test_the_human_summary_names_the_winner_and_the_disputed_cases(
        self, cli, suite_file, answering_arms
    ):
        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML), "--arm", "good", "--arm", "bad"
        )

        assert "winner: bedrock:good" in result.err
        assert "models disagree on: refund-window, store-hours" in result.err
        assert "MODEL" in result.err and "PASSED" in result.err
        assert "2/2 (100%)" in result.err

    def test_a_provider_prefix_picks_the_arms_provider(self, cli, suite_file, answering_arms):
        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML),
            "--arm", "openai=good", "--arm", "bedrock=good",
        )  # fmt: skip

        assert result.code == 0, result.err
        assert [run.provider for run in answering_arms] == ["openai"] * 2 + ["bedrock"] * 2
        assert [arm["label"] for arm in result.json()["arms"]] == ["openai:good", "bedrock:good"]

    def test_a_model_id_containing_colons_is_not_mistaken_for_a_provider(
        self, cli, suite_file, monkeypatch
    ):
        seen: list = []
        monkeypatch.setattr(
            commands,
            "build_model",
            lambda request, settings: seen.append(request) or FakeModel([Text("30 days Sunday")]),
        )

        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML),
            "--arm", "anthropic.claude-3:0", "--arm", "llama3.1:8b",
        )  # fmt: skip

        assert result.code == 0, result.err
        assert {run.model_id for run in seen} == {"anthropic.claude-3:0", "llama3.1:8b"}
        assert {run.provider for run in seen} == {"bedrock"}

    def test_json_mode_streams_every_arms_events_then_one_comparison_line(
        self, cli, suite_file, answering_arms
    ):
        result = cli(
            "--json", "eval", "--suite", suite_file(ASSERTING_SUITE_YAML),
            "--arm", "good", "--arm", "bad",
        )  # fmt: skip

        assert result.code == 0, result.err
        events = result.ndjson()
        assert [e["type"] for e in events].count("eval_complete") == 2
        assert events[-1]["type"] == "comparison"
        assert events[-1]["winner"] == "bedrock:good"

    def test_an_arm_that_fails_is_reported_and_the_others_still_run(
        self, cli, suite_file, monkeypatch
    ):
        def build(request, settings):
            if request.model_id == "broken":
                raise AppError("no such model", code="unknown_model")
            return FakeModel([Text("30 days Sunday")])

        monkeypatch.setattr(commands, "build_model", build)

        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML),
            "--arm", "broken", "--arm", "good",
        )  # fmt: skip

        assert result.code == 1
        comparison = result.json()
        assert comparison["winner"] == "bedrock:good"
        broken = next(arm for arm in comparison["arms"] if arm["model_id"] == "broken")
        assert broken["status"] == "error"
        assert comparison["ranking"] == ["bedrock:good"]
        assert "not ranked (error)" in result.err

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            (["--model", "m"], "drop --model"),
            (["--junit", "out.xml"], "cannot apply to a comparison"),
            (["--gate"], "it ranks models, it does not gate one"),
            (["--fail-under", "50"], "--fail-under cannot apply to a comparison"),
            (["--fail-on-case-failure"], "cannot apply to a comparison"),
        ],
    )
    def test_flags_that_make_a_comparison_ambiguous_are_refused(
        self, cli, suite_file, answering_arms, extra, message
    ):
        result = cli("eval", "--suite", suite_file(ASSERTING_SUITE_YAML), "--arm", "good", *extra)

        assert result.code == 2
        assert message in result.err
        assert result.out == ""
        assert answering_arms == []

    def test_arms_need_a_suite(self, cli):
        result = cli("eval", "-m", "m", "-p", "hi", "--arm", "good")

        assert result.code == 2
        assert "--arm compares models on a suite: pass --suite FILE" in result.err

    def test_the_same_model_twice_is_refused_before_anything_runs(
        self, cli, suite_file, answering_arms
    ):
        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML), "--arm", "good", "--arm", "good"
        )

        assert result.code == 2
        assert "bedrock:good is already an arm" in result.err
        assert answering_arms == []

    def test_a_bad_suite_file_is_refused_before_any_arm_runs(self, cli, suite_file, answering_arms):
        result = cli("eval", "--suite", suite_file("cases: [unclosed"), "--arm", "good")

        assert result.code == 2
        assert answering_arms == []


# --------------------------------------------------------------------------- #
# eval --suite --panel-judge: several judges grade every answer
# --------------------------------------------------------------------------- #


@pytest.fixture
def built_judges(monkeypatch):
    """The ``(model_id, provider)`` of every judge the CLI builds, in order."""
    from nimbus.evals.judge import FakeJudgeModel

    seen: list[tuple[str, str]] = []

    def build(model_id, settings, provider="bedrock"):
        seen.append((model_id, provider))
        return FakeJudgeModel(model_id=model_id)

    monkeypatch.setattr(commands, "build_judge_model", build)
    return seen


class TestPanel:
    def test_panel_judges_each_grade_and_the_result_says_so(
        self, cli, suite_file, built_judges
    ):
        result = cli(
            "eval", "--suite", suite_file(), "--grader-model", "judge-a",
            "--panel-judge", "judge-b", "--panel-judge", "openai=gpt-x",
        )  # fmt: skip

        assert result.code == 0, result.err
        assert sorted(built_judges) == [
            ("gpt-x", "openai"),
            ("judge-a", "bedrock"),
            ("judge-b", "bedrock"),
        ]
        judge = result.json()["result"]["judge"]
        assert judge["model_id"] == "judge-a"
        assert judge["panel"] == ["bedrock:judge-b", "openai:gpt-x"]
        assert "judge_spread" in result.json()["result"]["cases"][0]

    def test_a_suite_file_can_name_the_panel(self, cli, suite_file, built_judges):
        text = SUITE_YAML + "grader: {model_id: judge-a}\npanel:\n  - {model_id: judge-b}\n"

        result = cli("eval", "--suite", suite_file(text))

        assert result.code == 0, result.err
        assert result.json()["result"]["judge"]["panel"] == ["bedrock:judge-b"]

    def test_the_flag_replaces_the_files_panel(self, cli, suite_file, built_judges):
        text = SUITE_YAML + "grader: {model_id: judge-a}\npanel:\n  - {model_id: from-file}\n"

        result = cli("eval", "--suite", suite_file(text), "--panel-judge", "from-flag")

        assert result.code == 0, result.err
        assert result.json()["result"]["judge"]["panel"] == ["bedrock:from-flag"]
        assert ("from-file", "bedrock") not in built_judges

    def test_a_panel_judge_needs_a_suite(self, cli):
        result = cli("eval", "-m", "m", "-p", "hi", "--panel-judge", "judge-b")

        assert result.code == 2
        assert "--panel-judge adds judges to a suite's grading: pass --suite FILE" in result.err

    def test_the_primary_judge_cannot_also_sit_on_the_panel(self, cli, suite_file, built_judges):
        result = cli(
            "eval", "--suite", suite_file(), "--grader-model", "judge-a",
            "--panel-judge", "judge-a",
        )  # fmt: skip

        assert result.code == 2
        assert "Every judge must be a different model; repeated: bedrock:judge-a" in result.err
        assert built_judges == []

    def test_a_comparison_can_use_a_panel(self, cli, suite_file, answering_arms, built_judges):
        result = cli(
            "eval", "--suite", suite_file(ASSERTING_SUITE_YAML), "--arm", "good", "--arm", "bad",
            "--panel-judge", "judge-b",
        )  # fmt: skip

        assert result.code == 0, result.err
        assert result.json()["winner"] == "bedrock:good"


# --------------------------------------------------------------------------- #
# promote: a stored run becomes a suite case
# --------------------------------------------------------------------------- #


@pytest.fixture
def stored_run_id(cli):
    """The id of a run this test's history holds."""
    result = cli("--json", "run", "-m", "m1", "-p", "How long do I have to return shoes?")
    assert result.code == 0, result.err
    return next(e["run_id"] for e in result.ndjson() if e["type"] == "run_start")


class TestPromote:
    def test_it_prints_the_case_to_paste_and_writes_nothing(self, cli, stored_run_id, tmp_path):
        result = cli("promote", stored_run_id, "--id", "returns")

        assert result.code == 0, result.err
        assert result.out.splitlines()[0] == "  - id: returns"
        assert "How long do I have to return shoes?" in result.out
        assert "expected" not in result.out
        assert list(tmp_path.glob("*.yaml")) == []

    def test_the_recorded_answer_is_only_used_as_the_reference_on_request(
        self, cli, stored_run_id
    ):
        assert "expected:" not in cli("promote", stored_run_id).out
        assert "expected:" in cli("promote", stored_run_id, "--expected").out

    def test_it_adds_the_case_to_a_suite_leaving_the_rest_of_the_file_alone(
        self, cli, stored_run_id, suite_file
    ):
        path = suite_file(SUITE_YAML)

        result = cli("promote", stored_run_id, "--suite", path, "--id", "returns")

        assert result.code == 0, result.err
        assert "added case to" in result.err
        updated = Path(path).read_text(encoding="utf-8")
        assert updated.startswith(SUITE_YAML)
        assert "id: returns" in updated

    def test_a_promoted_case_runs_in_the_suite_it_was_added_to(
        self, cli, stored_run_id, suite_file, captured_runs
    ):
        path = suite_file(SUITE_YAML)
        assert cli("promote", stored_run_id, "--suite", path, "--id", "returns").code == 0

        result = cli("eval", "--suite", path)

        assert result.code == 0, result.err
        assert "How long do I have to return shoes?" in {run.user_prompt for run in captured_runs}
        assert len(result.json()["result"]["cases"]) == 3

    def test_a_missing_file_is_created_configured_as_the_run_was(
        self, cli, stored_run_id, tmp_path, captured_runs
    ):
        path = tmp_path / "fresh.yaml"

        result = cli("promote", stored_run_id, "--suite", str(path), "--id", "returns")

        assert result.code == 0, result.err
        assert "created" in result.err
        assert "model_id: m1" in path.read_text(encoding="utf-8")
        assert cli("eval", "--suite", str(path)).code == 0

    def test_a_case_id_already_in_the_suite_is_refused_and_the_file_is_untouched(
        self, cli, stored_run_id, suite_file
    ):
        path = suite_file(SUITE_YAML)

        result = cli("promote", stored_run_id, "--suite", path, "--id", "refund-window")

        assert result.code != 0
        assert "already has a case with id 'refund-window'" in result.err
        assert Path(path).read_text(encoding="utf-8") == SUITE_YAML

    def test_a_suite_it_cannot_safely_extend_is_refused_with_the_case_to_add_by_hand(
        self, cli, stored_run_id, suite_file
    ):
        path = suite_file(SUITE_YAML + "pass_threshold: 0.5\n")

        result = cli("promote", stored_run_id, "--suite", path)

        assert result.code != 0
        assert "not the last key" in result.err
        assert f"`nimbus promote {stored_run_id}` prints the case to add" in result.err

    def test_an_unknown_run_is_an_error(self, cli):
        result = cli("promote", "no-such-run")

        assert result.code != 0
        assert "no-such-run" in result.err

    def test_expected_needs_a_run_that_produced_an_answer(self, cli, monkeypatch):
        def failing(request, settings):
            raise AppError("no model", code="unknown_model")

        monkeypatch.setattr(runner, "build_model", failing)  # `run` builds through the runner
        run = cli("--json", "run", "-m", "m1", "-p", "hi")
        run_id = next(e["run_id"] for e in run.ndjson() if e["type"] == "run_start")

        result = cli("promote", run_id, "--expected")

        assert result.code != 0
        assert "has no output" in result.err
