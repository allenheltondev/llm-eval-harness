"""Tests for the command-line interface (evalharness.cli).

These drive the real entry point -- ``main(argv, stdin=, stdout=, stderr=)`` --
against the scripted fake model and a tmp_path sqlite file, so a passing test
means the whole path works: argparse, prompt resolution, the engine, the store,
the rendering and the exit code. Nothing here mocks the engine.

The two output streams are asserted separately on purpose. "stdout is the
product, stderr is the commentary" is the CLI's central promise, and it is only
worth anything if something checks it.
"""

import io
import json

import pytest

from evalharness.cli import commands
from evalharness.cli.main import COMMANDS, main
from evalharness.errors import AppError, NotFoundError
from evalharness.models_catalog import CatalogResult
from evalharness.store import db as store_db
from evalharness.store import repo as store_repo

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
    monkeypatch.setenv("EVALHARNESS_FAKE_MODEL", "1")
    # The sqlite engine is a module-level singleton that outlives a test, so a
    # test asserting on *which* database was opened has to start from unset.
    monkeypatch.setattr(store_db, "_engine", None)
    store_repo.reset_cache()
    yield
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
        assert "usage: evalharness" in result.err

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

        from evalharness.engine import runner
        from evalharness.engine.fake_model import Error, FakeModel, Text

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
        from evalharness.evals.jobs import to_json_line

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
        assert result.err == "evalharness: No run or evaluation with id 'nope'\n"


# --------------------------------------------------------------------------- #
# serve, and the top-level failure paths
# --------------------------------------------------------------------------- #


def test_serve_starts_uvicorn_on_the_requested_address(cli, monkeypatch):
    calls = {}

    def fake_run(target, **kwargs):
        calls["target"] = target
        calls.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules, "uvicorn", type("M", (), {"run": staticmethod(fake_run)})
    )
    result = cli("serve", "--host", "0.0.0.0", "--port", "9999")

    assert result.code == 0
    assert calls["target"] == "evalharness.main:app"
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 9999
    assert calls["reload"] is False
    assert "http://0.0.0.0:9999" in result.err


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
    assert result.err == "evalharness: nothing here\n"
    assert isinstance(NotFoundError("x"), AppError)


def test_the_default_database_is_used_when_no_db_is_passed(monkeypatch, tmp_path):
    """--db is an override; without it the store follows the environment."""
    monkeypatch.setenv("EVALHARNESS_FAKE_MODEL", "1")
    monkeypatch.setenv("EVALHARNESS_DB_PATH", str(tmp_path / "from-env.db"))
    monkeypatch.setattr(store_db, "_engine", None)
    store_repo.reset_cache()
    out, err = io.StringIO(), io.StringIO()
    assert main(["run", "-m", "m1", "-p", "hi"], stdin=_Stdin(), stdout=out, stderr=err) == 0
    assert (tmp_path / "from-env.db").exists()


def test_the_module_entry_point_exposes_the_same_main():
    """`python -m evalharness.cli` and the console script are one entry point."""
    from evalharness.cli import __main__

    assert __main__.main is main
