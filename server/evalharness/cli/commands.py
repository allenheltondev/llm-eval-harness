"""The command bodies, one coroutine per subcommand.

Every command drives the *same* engine the HTTP API drives -- ``execute_run``
for a run, ``execute_evaluation_with_seam`` for an evaluation -- with no server
in the path and no HTTP client. The API and the CLI are two front doors onto
one core, which is the point: anything the UI can do is reachable from a shell,
and neither door can drift into being the "real" one.

Each command takes its parsed arguments plus the two output streams and returns
a process exit code. Nothing here calls ``sys.exit`` or touches ``sys.stdout``
directly, so the whole surface is testable with two ``StringIO``s.
"""

from __future__ import annotations

import argparse
from contextlib import aclosing
from typing import Any, TextIO

from evalharness.awscat.catalog import ModelCatalog
from evalharness.cli import render
from evalharness.config import Settings
from evalharness.engine.events import (
    ErrorEvent,
    MetricsEvent,
    RunCompleteEvent,
    TextDeltaEvent,
)
from evalharness.engine.model_factory import build_model
from evalharness.engine.runner import execute_run
from evalharness.engine.schemas import GuardrailConfig, InferenceConfig, RunRequest
from evalharness.errors import NotFoundError
from evalharness.evals.engine import EvalDeps, LocalEvalStore, execute_evaluation_with_seam
from evalharness.evals.jobs import to_json_line
from evalharness.evals.judge import build_judge_model
from evalharness.evals.schemas import EvaluationRequest, GraderConfig
from evalharness.models_catalog import ProviderCatalog
from evalharness.providers import PROVIDERS, is_configured
from evalharness.schemas.runs import EvaluationDetail, RunDetail
from evalharness.store.repo import HistoryRepo, get_history_repo
from evalharness.tools.registry import list_handlers

#: The run or evaluation finished, whatever its verdict.
EXIT_OK = 0
#: The run or evaluation settled as ``error``. A *grade* of F is still ``0``:
#: the harness did its job. This code means the harness could not.
EXIT_FAILED = 1
#: Cancelled, by Ctrl-C or otherwise. Matches the shell's 128+SIGINT.
EXIT_CANCELLED = 130

#: The events that only ever arrive once the model has stopped producing text.
#: Seeing one is what tells the renderer it is safe to terminate stdout's line
#: before progress starts printing, so the summary does not run on from the
#: end of the answer on a shared terminal. Tool results are deliberately absent:
#: a tool call happens *between* stretches of text, and breaking the line there
#: would put a newline into the middle of redirected output.
_ENDS_TEXT = (MetricsEvent, ErrorEvent, RunCompleteEvent)

#: Terminal statuses mapped to the exit code they produce.
_EXIT_FOR_STATUS = {
    "completed": EXIT_OK,
    "error": EXIT_FAILED,
    "cancelled": EXIT_CANCELLED,
}


def _write(stream: TextIO, text: str) -> None:
    """Write and flush, so a streaming run actually streams."""
    stream.write(text)
    stream.flush()


def _note(err: TextIO, line: str | None) -> None:
    if line is not None:
        _write(err, line + "\n")


# --------------------------------------------------------------------------- #
# Building engine requests from parsed arguments
# --------------------------------------------------------------------------- #


def build_run_request(args: argparse.Namespace) -> RunRequest:
    """The ``RunRequest`` these arguments describe.

    Deliberately produces the very model ``POST /runs`` validates, so an
    invalid combination (a guardrail on a non-Bedrock provider, say) fails here
    with the same message and the same reasoning as it would over HTTP.
    """
    guardrail = (
        GuardrailConfig(id=args.guardrail_id, version=args.guardrail_version)
        if args.guardrail_id
        else None
    )
    return RunRequest(
        model_id=args.model,
        provider=args.provider,
        system_prompt=args.system or "",
        user_prompt=args.prompt,
        inference=InferenceConfig(
            temperature=args.temperature,
            top_p=args.top_p,
            max_tokens=args.max_tokens,
        ),
        toolset=args.toolset,
        max_tool_iterations=args.max_tool_iterations,
        guardrail=guardrail,
        # A CLI run is always consumed as a stream: that is what makes the text
        # appear as it is generated rather than in one lump at the end.
        stream=True,
    )


def build_eval_request(args: argparse.Namespace) -> EvaluationRequest:
    """The ``EvaluationRequest`` these arguments describe.

    ``--run`` (repeatable) selects ``kind="grade"`` over already-stored runs;
    without it this is a determinism experiment over ``-n`` fresh repeats.
    """
    # `model_id` is only passed when it was actually given: GraderConfig's own
    # default is the built-in judge model, and handing it an explicit None
    # would fail validation rather than fall back to it.
    grader = GraderConfig(
        provider=args.grader_provider,
        system_prompt=args.grader_system,
        **({"model_id": args.grader_model} if args.grader_model else {}),
    )
    if args.run:
        return EvaluationRequest(
            kind="grade",
            run_ids=list(args.run),
            rubric=args.rubric,
            grader=grader,
        )
    return EvaluationRequest(
        kind="determinism",
        run_config=build_run_request(args),
        n=args.n,
        rubric=args.rubric,
        grader=grader,
    )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Execute one run, streaming its text to stdout and its progress to stderr.

    ``aclosing`` is load-bearing exactly as it is in the router: on Ctrl-C the
    generator must be closed promptly so the engine's cancellation path runs
    and the row is persisted as ``cancelled`` rather than left ``running``.
    """
    request = build_run_request(args)
    events = execute_run(request, settings=settings, repo=get_history_repo(settings))
    status = "error"
    wrote_text = False

    async with aclosing(events):
        async for event in events:
            if args.json:
                _write(out, event.to_json_line())
            elif isinstance(event, TextDeltaEvent):
                _write(out, event.text)
                wrote_text = wrote_text or event.text != ""
            else:
                if wrote_text and isinstance(event, _ENDS_TEXT):
                    _write(out, "\n")
                    wrote_text = False
                _note(err, render.run_progress_line(event))

            if isinstance(event, RunCompleteEvent):
                status = event.status

    # No fallback newline is needed after the loop: `run_complete` is in
    # _ENDS_TEXT and the engine always yields it on every path that reaches
    # here. (The paths that do not -- disconnect, cancellation -- re-raise out
    # of the generator, so nothing after this block would run anyway.)
    return _EXIT_FOR_STATUS.get(status, EXIT_FAILED)


# --------------------------------------------------------------------------- #
# eval
# --------------------------------------------------------------------------- #


async def evaluate(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Run one evaluation in the foreground and print its result.

    The HTTP API answers ``202`` and runs the job in the background because a
    request cannot wait fifteen minutes. A CLI invocation *is* the job, so this
    drives the engine's seam directly: no job registry, no polling, and Ctrl-C
    means cancel this evaluation rather than "stop watching it".
    """
    request = build_eval_request(args)
    repo = get_history_repo(settings)

    if request.kind == "grade":
        # Same synchronous check the router makes: an unknown run id is an error
        # now, not a job that fails a few seconds in.
        for run_id in request.run_ids:
            repo.get_run(run_id)

    record = repo.create_evaluation(
        kind=request.kind,
        run_ids=request.run_ids if request.kind == "grade" else [],
        config=request.stored_config(),
        status="pending",
    )

    def emit(entry: dict[str, Any]) -> None:
        if args.json:
            # The job log's own serializer, so `--json` emits byte-for-byte the
            # lines `GET /evaluations/{id}/events` serves.
            _write(out, to_json_line(entry))
        else:
            _note(err, render.eval_progress_line(entry))

    terminal = await execute_evaluation_with_seam(
        request,
        emit,
        LocalEvalStore(record.id, repo),
        deps=EvalDeps(
            settings=settings,
            model_factory=lambda req: build_model(req, settings),
            judge_factory=lambda model_id, provider="bedrock": build_judge_model(
                model_id, settings, provider
            ),
            repo=repo,
        ),
        evaluation_id=record.id,
    )

    if not args.json:
        for line in render.eval_result_lines(terminal.get("result")):
            _write(err, line + "\n")
        _write(out, render.dumps(terminal) + "\n")

    return _EXIT_FOR_STATUS.get(str(terminal.get("status")), EXIT_FAILED)


# --------------------------------------------------------------------------- #
# Discovery: models, tools
# --------------------------------------------------------------------------- #


async def models(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """List every model reachable from this machine, across every provider.

    Same aggregation as ``GET /models``, including its degrade-to-empty
    behaviour: a provider that fails to list contributes nothing and does not
    fail the command.
    """
    result = await ProviderCatalog().collect(settings, ModelCatalog())

    if args.json:
        _write(
            out,
            render.dumps(
                {"models": result.models, "providers": result.providers, "cached": result.cached}
            )
            + "\n",
        )
        return EXIT_OK

    rows = [
        [entry.get("source", ""), entry.get("model_id", ""), entry.get("name", "")]
        for entry in result.models
    ]
    if rows:
        _write(out, render.table(rows, ["PROVIDER", "MODEL ID", "NAME"]) + "\n")

    unconfigured = [name for name in PROVIDERS if not is_configured(name, settings)]
    if unconfigured:
        _note(err, f"| not configured: {', '.join(unconfigured)}")
    if not rows:
        _note(err, "| no models available")
    return EXIT_OK


async def tools(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """List the toolsets a run may name with ``--toolset``."""
    handlers = list_handlers()
    if args.json:
        _write(
            out,
            render.dumps(
                {"toolsets": [{"name": name, "tools": names} for name, names in handlers.items()]}
            )
            + "\n",
        )
        return EXIT_OK

    rows = [[name, ", ".join(names)] for name, names in handlers.items()]
    if rows:
        _write(out, render.table(rows, ["TOOLSET", "TOOLS"]) + "\n")
    else:
        _note(err, "| no toolsets registered")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# History: runs, show
# --------------------------------------------------------------------------- #


async def runs(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """List stored runs, newest first."""
    repo: HistoryRepo = get_history_repo(settings)
    records, next_cursor = repo.list_runs(
        model_id=args.model, status=args.status, limit=args.limit, cursor=args.cursor
    )

    if args.json:
        items = [RunDetail.model_validate(record).model_dump() for record in records]
        _write(out, render.dumps({"items": items, "next_cursor": next_cursor}) + "\n")
        return EXIT_OK

    rows = [
        [
            record.id,
            record.ts.isoformat(timespec="seconds"),
            record.status,
            record.model_id,
        ]
        for record in records
    ]
    if rows:
        _write(out, render.table(rows, ["ID", "WHEN", "STATUS", "MODEL"]) + "\n")
    else:
        _note(err, "| no runs stored")
    if next_cursor:
        _note(err, f"| more: --cursor {next_cursor}")
    return EXIT_OK


async def show(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Print one stored run or evaluation as JSON.

    Runs and evaluations share an id namespace from the caller's point of view
    -- they have an id and they want to see it -- so this looks in both rather
    than making them remember which kind of thing they are holding.
    """
    repo: HistoryRepo = get_history_repo(settings)
    try:
        record = repo.get_run(args.id)
    except NotFoundError:
        try:
            evaluation = repo.get_evaluation(args.id)
        except NotFoundError:
            # Report what was actually asked. "No evaluation with that id" is a
            # confusing thing to hear back when you typed a run id.
            raise NotFoundError(f"No run or evaluation with id {args.id!r}") from None
        _write(out, render.dumps(EvaluationDetail.model_validate(evaluation).model_dump()) + "\n")
        return EXIT_OK

    _write(out, render.dumps(RunDetail.model_validate(record).model_dump()) + "\n")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


async def serve(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Boot the HTTP API, which is what the web UI talks to.

    The UI is one way to use the harness, not the way, so starting it is a
    subcommand rather than a separate entry point people have to learn.
    """
    import uvicorn

    _note(err, f"| serving evalharness on http://{args.host}:{args.port}")
    uvicorn.run(
        "evalharness.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return EXIT_OK
