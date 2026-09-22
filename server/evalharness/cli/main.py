"""``evalharness`` -- the command-line entry point.

The CLI is the primary way to drive the harness: one run, one evaluation, the
history behind them, and the model/toolset catalogues that say what is
available. ``evalharness serve`` starts the HTTP API for anyone who would
rather click, which makes the web UI one front door rather than the front door.

Nothing here talks HTTP. Commands call the same engine functions the routers
call, against the same history store, so a local run and a run submitted to a
deployed server differ in where they execute and nothing else.

Conventions this file exists to enforce:

* **stdout is the product, stderr is the commentary.** Redirecting stdout gets
  you the model's text, the JSON document, or the table -- never progress.
* **``--json`` on every command.** Human output is for humans; ``--json`` is
  the contract for scripts and for anything wrapping this as an MCP server.
* **Exit codes mean something.** ``0`` finished, ``1`` the harness failed,
  ``2`` the invocation was wrong, ``130`` cancelled. An ``F`` grade is ``0``:
  the evaluation succeeded in telling you the answer is bad.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from evalharness.cli import commands
from evalharness.cli.commands import EXIT_CANCELLED, EXIT_FAILED
from evalharness.config import Settings
from evalharness.errors import AppError
from evalharness.evals.judge import DEFAULT_JUDGE_MODEL_ID
from evalharness.providers import PROVIDERS
from evalharness.store.db import init_db

#: Exit code argparse itself uses for a bad invocation; reused for the checks
#: argparse cannot express (a prompt that is required only sometimes).
EXIT_USAGE = 2

#: A command returns an exit code, or a coroutine yielding one. Both shapes are
#: allowed because `serve` has to run *outside* the asyncio wrapper -- see the
#: note on it in `commands.py`.
Command = Callable[[argparse.Namespace, Settings, TextIO, TextIO], int | Awaitable[int]]

COMMANDS: dict[str, Command] = {
    "run": commands.run,
    "eval": commands.evaluate,
    "models": commands.models,
    "tools": commands.tools,
    "runs": commands.runs,
    "show": commands.show,
    "serve": commands.serve,
}


class UsageError(Exception):
    """A bad invocation that argparse's own declarations cannot catch."""


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #


#: Help text kept in one place: the global options are declared twice (once on
#: the root parser, once on every subcommand) so that both `evalharness --json
#: run ...` and `evalharness run --json ...` work. People type both.
_JSON_HELP = "machine-readable output on stdout (NDJSON for streams, JSON otherwise)"
_DB_HELP = "sqlite history file (default: EVALHARNESS_DB_PATH, else ./data/evalharness.db)"


def _global_options() -> argparse.ArgumentParser:
    """``--db`` and ``--json`` as accepted *after* the subcommand.

    ``default=SUPPRESS`` is what makes the duplication safe: these only set an
    attribute when actually passed, so a subcommand that does not mention them
    leaves the root parser's value standing instead of overwriting it with a
    fresh default.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--db", type=Path, default=argparse.SUPPRESS, help=_DB_HELP)
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=_JSON_HELP)
    return parent


def _run_options() -> argparse.ArgumentParser:
    """Everything that describes a single run -- shared by ``run`` and ``eval``.

    ``--model`` is optional here rather than required because ``eval --run ID``
    grades stored runs and needs no model of its own; the commands that do need
    it say so themselves.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-m", "--model", help="model id, as listed by `evalharness models`")
    parent.add_argument(
        "--provider",
        choices=PROVIDERS,
        default="bedrock",
        help="which SDK executes the run (default: bedrock)",
    )
    parent.add_argument(
        "-p",
        "--prompt",
        help="the user prompt; '-' or omitted reads stdin",
    )
    parent.add_argument("-s", "--system", help="the system prompt")
    parent.add_argument(
        "--system-file",
        type=Path,
        help="read the system prompt from a file",
    )
    parent.add_argument("--toolset", help="a toolset from `evalharness tools`")
    parent.add_argument(
        "--max-tool-iterations",
        type=int,
        default=10,
        help="cap on agent loop turns (default: 10)",
    )
    parent.add_argument("--temperature", type=float, help="sampling temperature")
    parent.add_argument("--top-p", type=float, help="nucleus sampling cutoff")
    parent.add_argument("--max-tokens", type=int, help="cap on generated tokens")
    parent.add_argument("--guardrail-id", help="Bedrock guardrail id (Bedrock provider only)")
    parent.add_argument(
        "--guardrail-version",
        default="DRAFT",
        help="Bedrock guardrail version (default: DRAFT)",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree."""
    output = _global_options()
    run_options = _run_options()

    parser = argparse.ArgumentParser(
        prog="evalharness",
        description="Run and evaluate LLM prompts locally.",
    )
    parser.add_argument("--db", type=Path, default=None, help=_DB_HELP)
    parser.add_argument("--json", action="store_true", default=False, help=_JSON_HELP)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    run = subparsers.add_parser(
        "run",
        parents=[run_options, output],
        help="execute one run",
        description="Execute one run. The model's text goes to stdout, progress to stderr.",
    )
    run.set_defaults(requires_model=True)

    evaluate = subparsers.add_parser(
        "eval",
        parents=[run_options, output],
        help="run a determinism experiment, or grade stored runs",
        description=(
            "Repeat a prompt N times and grade the batch for determinism, or -- with "
            "--run -- grade runs that are already stored."
        ),
    )
    evaluate.add_argument(
        "-n",
        type=int,
        default=10,
        dest="n",
        help="repeats for a determinism experiment, 2-25 (default: 10)",
    )
    evaluate.add_argument(
        "--run",
        action="append",
        metavar="RUN_ID",
        help="grade this stored run instead of executing new ones; repeatable",
    )
    evaluate.add_argument("--rubric", help="extra rubric text for the judge")
    evaluate.add_argument(
        "--grader-model",
        default=None,
        help="the judge's model id (default: the built-in judge model)",
    )
    evaluate.add_argument(
        "--grader-provider",
        choices=PROVIDERS,
        default="bedrock",
        help="which SDK runs the judge (default: bedrock)",
    )
    evaluate.add_argument("--grader-system", help="override the judge's system prompt")

    subparsers.add_parser(
        "models", parents=[output], help="list available models across every provider"
    )
    subparsers.add_parser("tools", parents=[output], help="list the registered toolsets")

    runs = subparsers.add_parser("runs", parents=[output], help="list stored runs, newest first")
    runs.add_argument("--limit", type=int, default=20, help="rows per page (default: 20)")
    runs.add_argument("--model", help="only runs on this model id")
    runs.add_argument("--status", help="only runs with this status")
    runs.add_argument("--cursor", help="continue from a previous page")

    show = subparsers.add_parser(
        "show", parents=[output], help="print one stored run or evaluation as JSON"
    )
    show.add_argument("id", help="a run id or an evaluation id")

    serve = subparsers.add_parser("serve", parents=[output], help="start the HTTP API and UI")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    serve.add_argument("--reload", action="store_true", help="reload on source changes")
    serve.add_argument("--log-level", default="info", help="uvicorn log level (default: info)")

    return parser


# --------------------------------------------------------------------------- #
# Argument resolution
# --------------------------------------------------------------------------- #


def resolve_prompts(args: argparse.Namespace, stdin: TextIO) -> None:
    """Fill in ``prompt`` and ``system`` from stdin and ``--system-file``.

    Reading the prompt from stdin when it was not given makes the harness a
    normal member of a pipeline (``cat prompt.txt | evalharness run -m ...``).
    It is skipped when stdin is a terminal, because silently blocking on an
    invisible read is the single most confusing thing a CLI can do -- there the
    missing prompt is reported as the usage error it is.
    """
    if args.system_file is not None:
        try:
            args.system = args.system_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"--system-file: {exc.strerror}: {args.system_file}") from None

    if args.prompt is not None and args.prompt != "-":
        return
    if args.prompt is None and stdin.isatty():
        raise UsageError("a prompt is required: pass --prompt, or pipe one in on stdin")

    piped = stdin.read()
    if not piped.strip():
        raise UsageError("the prompt read from stdin was empty")
    # Kept verbatim, stripped only for the emptiness test above. Indentation and
    # trailing newlines are meaningful in the things people pipe in -- code,
    # markdown, delimiter-based templates -- and `--prompt` does not tidy its
    # argument either, so trimming here would make the same prompt mean two
    # different things depending on how it arrived.
    args.prompt = piped


def _check_required(args: argparse.Namespace) -> None:
    """The requirements that depend on other arguments."""
    if args.command == "run" and not args.model:
        raise UsageError("run needs a model: pass --model (see `evalharness models`)")
    if args.command == "eval" and not args.run and not args.model:
        raise UsageError(
            "eval needs either --model (to execute new runs) or --run (to grade stored ones)"
        )
    if args.command == "eval" and args.grader_provider != "bedrock" and not args.grader_model:
        # The judge's default model id is a Bedrock one, so inheriting it on
        # another provider builds a judge that can only fail at the provider.
        # No default is invented here: the right judge model for OpenAI or a
        # local Ollama is the caller's to name, and guessing would rot.
        raise UsageError(
            f"--grader-provider {args.grader_provider} needs an explicit --grader-model: "
            f"the default judge model ({DEFAULT_JUDGE_MODEL_ID}) is a Bedrock model id"
        )


def prepare(args: argparse.Namespace, stdin: TextIO) -> None:
    """Resolve and validate everything argparse could not decide on its own."""
    _check_required(args)
    # `eval --run` grades stored runs, so it needs no prompt of its own; every
    # other run-shaped invocation does.
    if args.command == "run" or (args.command == "eval" and not args.run):
        resolve_prompts(args, stdin)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse ``argv`` and run the named command. Returns the process exit code.

    The three streams are injectable so the whole CLI is testable without
    touching the process's own.
    """
    stdin = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(err)
        return EXIT_USAGE

    try:
        prepare(args, stdin)
    except UsageError as exc:
        err.write(f"evalharness: {exc}\n")
        return EXIT_USAGE

    # Settings() parses the whole EVALHARNESS_ environment and raises on a
    # malformed value, so it is built inside the guarded block: a typo in a
    # shell profile should reach the user as one diagnostic line and exit 2,
    # the same as any other bad input, rather than as a traceback out of a
    # command that had not started yet.
    try:
        if args.db is not None:
            # Exported, not just held in our own Settings: `serve` hands the app
            # to uvicorn by import string, and with --reload to a whole child
            # process, and the app's lifespan builds its own Settings from the
            # environment. The environment is the only override all three of
            # those can see, so anything less would have the CLI and the server
            # it just started reading different history stores.
            os.environ["EVALHARNESS_DB_PATH"] = str(args.db)
            # --db names a SQLite file, so it pins the backend too. Without
            # this, an environment carrying EVALHARNESS_HISTORY_BACKEND=dynamodb
            # would create the file and then write every run to DynamoDB
            # anyway -- the opposite of the isolated scratch store --db exists
            # to give you.
            os.environ["EVALHARNESS_HISTORY_BACKEND"] = "sqlite"
            init_db(str(args.db))
        settings = Settings()

        outcome = COMMANDS[args.command](args, settings, out, err)
        # `serve` is synchronous on purpose (uvicorn.run starts its own loop and
        # forks a reloader); everything else is a coroutine to be driven here.
        return asyncio.run(outcome) if inspect.isawaitable(outcome) else outcome
    except KeyboardInterrupt:
        err.write("\nevalharness: cancelled\n")
        return EXIT_CANCELLED
    except AppError as exc:
        err.write(f"evalharness: {exc.message}\n")
        return EXIT_FAILED
    except ValidationError as exc:
        # The request models enforce bounds argparse knows nothing about (a
        # temperature over 1.0, an empty prompt). Those are bad invocations, so
        # they read like one instead of like a crash.
        for problem in exc.errors():
            location = ".".join(str(part) for part in problem["loc"]) or "argument"
            err.write(f"evalharness: {location}: {problem['msg']}\n")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
