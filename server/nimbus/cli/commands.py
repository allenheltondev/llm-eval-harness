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
import asyncio
import contextlib
import getpass
import platform
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime
from importlib import resources
from typing import Annotated, Any, TextIO

from pydantic import Field, TypeAdapter, ValidationError

from nimbus.awscat.catalog import ModelCatalog
from nimbus.cli import diagnose, remote, render
from nimbus.config import Settings
from nimbus.engine.events import (
    ErrorEvent,
    MetricsEvent,
    RunCompleteEvent,
    RunEvent,
    TextDeltaEvent,
)
from nimbus.engine.model_factory import build_model
from nimbus.engine.runner import execute_run
from nimbus.engine.schemas import GuardrailConfig, InferenceConfig, RunRequest
from nimbus.errors import AppError, NotFoundError
from nimbus.evals.engine import EvalDeps, LocalEvalStore, execute_evaluation_with_seam
from nimbus.evals.jobs import to_json_line
from nimbus.evals.judge import build_judge_model
from nimbus.evals.schemas import (
    DEFAULT_DETERMINISM_RUNS,
    EvaluationRequest,
    GraderConfig,
)
from nimbus.models_catalog import ProviderCatalog
from nimbus.providers import DEFAULT_PROVIDER
from nimbus.schemas.runs import EvaluationDetail, RunDetail
from nimbus.store.repo import HistoryRepo, get_history_repo
from nimbus.tools.registry import list_handlers

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


def _on_stack(err: TextIO, login: remote.Login) -> None:
    """Say where a command is running, when it is not running here."""
    who = f" as {login.email}" if login.email else ""
    _note(err, f"| on {login.url}{who}")


#: Parses one NDJSON line of a run's stream back into the engine's own event.
_RUN_EVENT: TypeAdapter[RunEvent] = TypeAdapter(Annotated[RunEvent, Field(discriminator="type")])


async def _remote_run_events(login: remote.Login, body: dict[str, Any]) -> AsyncIterator[RunEvent]:
    """A run on the stack, as the same events a local run yields.

    An event type this CLI does not know (a newer server) is skipped rather
    than fatal: the stream's text and its ending are what matter here.
    """
    async with remote.http_client() as http:
        api = remote.RemoteApi(http, login)
        async for entry in api.run(body):
            try:
                yield _RUN_EVENT.validate_python(entry)
            except ValidationError:
                continue


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
        provider=args.provider or DEFAULT_PROVIDER,
        system_prompt=args.system or "",
        user_prompt=args.prompt,
        inference=InferenceConfig(
            temperature=args.temperature,
            top_p=args.top_p,
            max_tokens=args.max_tokens,
        ),
        toolset=args.toolset,
        **(
            {"max_tool_iterations": args.max_tool_iterations}
            if args.max_tool_iterations is not None
            else {}
        ),
        guardrail=guardrail,
        # A CLI run is always consumed as a stream: that is what makes the text
        # appear as it is generated rather than in one lump at the end.
        stream=True,
    )


def _run_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """The run settings actually given on the command line, as run_config fields.

    Only what was passed: every run option defaults to ``None`` precisely so
    this can tell "not given" from "given the default", and a suite file's own
    choice is never overridden by a flag nobody typed.
    """
    overrides: dict[str, Any] = {}
    for field, value in (
        ("model_id", args.model),
        ("provider", args.provider),
        ("system_prompt", args.system),
        ("toolset", args.toolset),
        ("max_tool_iterations", args.max_tool_iterations),
    ):
        if value is not None:
            overrides[field] = value
    inference = {
        name: value
        for name, value in (
            ("temperature", args.temperature),
            ("top_p", args.top_p),
            ("max_tokens", args.max_tokens),
        )
        if value is not None
    }
    if inference:
        overrides["inference"] = inference
    if args.guardrail_id:
        overrides["guardrail"] = {"id": args.guardrail_id, "version": args.guardrail_version}
    return overrides


def build_suite_request(args: argparse.Namespace) -> EvaluationRequest:
    """The ``kind="suite"`` request for ``eval --suite FILE``.

    The file supplies the suite; flags override it. ``rubric`` and ``grader``
    live at the file's top level (they are the request's, not the suite's), and
    ``--rubric`` / ``--grader-*`` override those. Validation is the same model
    ``POST /evaluations`` uses, so a file that works here works over HTTP.
    """
    spec = dict(args.suite_spec)
    rubric = spec.pop("rubric", None)
    grader = dict(spec.pop("grader", None) or {})

    run_config = dict(spec.get("run_config") or {})
    overrides = _run_overrides(args)
    if "inference" in overrides:
        overrides["inference"] = {**(run_config.get("inference") or {}), **overrides["inference"]}
    spec["run_config"] = {**run_config, **overrides}

    for field, value in (
        ("model_id", args.grader_model),
        ("provider", args.grader_provider),
        ("system_prompt", args.grader_system),
    ):
        if value is not None:
            grader[field] = value

    return EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": spec,
            "rubric": args.rubric if args.rubric is not None else rubric,
            "grader": grader,
            "source": "cli",
        }
    )


def build_eval_request(args: argparse.Namespace) -> EvaluationRequest:
    """The ``EvaluationRequest`` these arguments describe.

    ``--suite FILE`` runs a test suite; ``--run`` (repeatable) selects
    ``kind="grade"`` over already-stored runs; otherwise this is a determinism
    experiment over ``-n`` fresh repeats.
    """
    if getattr(args, "suite_spec", None) is not None:
        return build_suite_request(args)
    # `model_id` is only passed when it was actually given: GraderConfig's own
    # default is the built-in judge model, and handing it an explicit None
    # would fail validation rather than fall back to it.
    grader = GraderConfig(
        provider=args.grader_provider or DEFAULT_PROVIDER,
        system_prompt=args.grader_system,
        **({"model_id": args.grader_model} if args.grader_model else {}),
    )
    if args.run:
        return EvaluationRequest(
            kind="grade",
            run_ids=list(args.run),
            rubric=args.rubric,
            grader=grader,
            source="cli",
        )
    return EvaluationRequest(
        kind="determinism",
        run_config=build_run_request(args),
        n=args.n if args.n is not None else DEFAULT_DETERMINISM_RUNS,
        rubric=args.rubric,
        grader=grader,
        source="cli",
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
    target: remote.Login | None = getattr(args, "target", None)
    if target is not None:
        _on_stack(err, target)
        events = _remote_run_events(target, request.model_dump(mode="json", by_alias=True))
    else:
        events = execute_run(request, settings=settings, repo=get_history_repo(settings))
    status = "error"
    # True when text has been written whose last character is not a newline.
    # Tracked rather than "has any text been written" because a model that ends
    # its answer with a newline -- markdown and code answers routinely do --
    # needs no second one, and adding it would break the answer-plus-exactly-
    # one-newline contract that makes `run ... > answer.txt` produce a clean
    # file.
    needs_newline = False

    async with aclosing(events):
        async for event in events:
            if args.json:
                _write(out, event.to_json_line())
            elif isinstance(event, TextDeltaEvent):
                if event.text:
                    _write(out, event.text)
                    needs_newline = not event.text.endswith("\n")
            else:
                if needs_newline and isinstance(event, _ENDS_TEXT):
                    _write(out, "\n")
                    needs_newline = False
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
    if getattr(args, "target", None) is not None or getattr(args, "remote", False):
        try:
            return await evaluate_remote(args, request, out, err)
        except _RunsNotOnStack as missing:
            # `eval --run` with ids from this machine's history: the stack has
            # never seen them, so grade them where they are -- and say so.
            _note(err, f"| {missing} on {args.target.url}; grading on this machine instead")
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
# eval on a stack, login, logout, whoami
# --------------------------------------------------------------------------- #


class _RunsNotOnStack(Exception):
    """``eval --run`` named runs the stack does not have; the message is the stack's."""


def _choose_lane(health: dict[str, Any], url: str) -> str:
    """The lane the server's own UI would pick: the cloud one when it has it.

    A deployed server has no local lane at all; a laptop's ``serve`` usually
    has no cloud lane. Asked rather than assumed, so ``--remote`` works against
    either without a flag.
    """
    if (health.get("cloud_evals") or {}).get("configured"):
        return "cloud"
    if (health.get("local_evals") or {}).get("available", True):
        return "local"
    raise remote.RemoteError(f"{url} has no evaluation lane configured")


async def evaluate_remote(
    args: argparse.Namespace, request: EvaluationRequest, out: TextIO, err: TextIO
) -> int:
    """Run the evaluation on the signed-in stack and follow it from here.

    The server owns it -- it runs there, is stored there, and is listed in that
    server's UI -- and this command renders its event stream exactly as it
    renders a local one. Ctrl-C cancels it on the server, as it would locally;
    ``--detach`` submits and returns instead of following.
    """
    login = getattr(args, "target", None)
    if login is None:
        raise remote.NotSignedInError(
            "not signed in to a stack: run `nimbus login --url https://<your-stack>`"
        )

    async with remote.http_client() as http:
        api = remote.RemoteApi(http, login)
        execution = _choose_lane(await api.health(), login.url)
        body = request.model_copy(update={"execution": execution}).model_dump(mode="json")
        try:
            created = await api.submit(body)
        except remote.RemoteNotFoundError as exc:
            # Refused before anything started, so falling back cannot run
            # anything twice. Only when the stack was the default, not asked for.
            if request.kind == "grade" and not getattr(args, "remote", False):
                raise _RunsNotOnStack(exc.message) from None
            raise
        evaluation_id = created["id"]
        link = remote.evaluation_link(login.url, evaluation_id)
        _note(err, f"| evaluation {evaluation_id} submitted to {login.url} ({execution} lane)")
        _note(err, f"| {link}")

        if args.detach:
            _write(out, render.dumps({"evaluation_id": evaluation_id, "url": link}) + "\n")
            return EXIT_OK

        try:
            async for entry in api.events(evaluation_id):
                if args.json:
                    _write(out, to_json_line(entry))
                else:
                    _note(err, render.eval_progress_line(entry))
        except asyncio.CancelledError:
            # Ctrl-C: the evaluation is the server's, so stopping here would
            # leave it running with nobody watching. Cancel it there too.
            _note(err, f"| cancelling {evaluation_id} on the server")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(api.cancel(evaluation_id), timeout=10)
            raise

        final = await api.evaluation(evaluation_id)

    terminal = {
        "evaluation_id": evaluation_id,
        "status": final.get("status"),
        "result": final.get("result"),
        "error": final.get("error"),
        "run_ids": final.get("run_ids") or [],
        "url": link,
    }
    if not args.json:
        for line in render.eval_result_lines(terminal["result"]):
            _write(err, line + "\n")
        _write(out, render.dumps(terminal) + "\n")
        _note(err, f"| {link}")
    return _EXIT_FOR_STATUS.get(str(terminal["status"]), EXIT_FAILED)


def _prompt(err: TextIO, stdin: TextIO, label: str) -> str:
    _write(err, label)
    err.flush()
    return stdin.readline().strip()


async def login(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Sign in to a stack and save the login; commands run there from then on.

    The password comes from the terminal without echo, or from stdin with
    ``--password-stdin`` (for scripts); it is sent to the user pool and never
    stored. An invited user's first sign-in sets their permanent password here.
    """
    stdin: TextIO = args.stdin
    previous = remote.load_login()
    raw_url = args.url or (previous.url if previous else None)
    if not raw_url:
        raise remote.RemoteError("which server? pass --url https://<your-stack>")
    url = remote.normalize_url(raw_url)

    async with remote.http_client() as http:
        health = await remote.discover(http, url)
        auth = health.get("auth") or {}
        if not auth.get("required"):
            remote.save_login(remote.Login(url=url))
            _note(err, f"| {url} does not require sign-in; commands now run there")
            _note(err, "| pass --local to use this machine, or `nimbus logout` to stop")
            if args.json:
                _write(out, render.dumps({"url": url, "email": None}) + "\n")
            return EXIT_OK

        # Before anything is asked for: a pool this server made up gets no password.
        pool = remote.checked_pool(auth, url)
        email = args.email or (previous.email if previous and previous.url == url else None)
        if not email:
            if not stdin.isatty():
                raise remote.RemoteError("pass --email when not signing in from a terminal")
            email = _prompt(err, stdin, "Email: ")
        if args.password_stdin:
            password = stdin.readline().rstrip("\n")
        elif stdin.isatty():
            password = getpass.getpass("Password: ", stream=err)
        else:
            raise remote.RemoteError("pass --password-stdin to read the password from stdin")
        if not email or not password:
            raise remote.RemoteError("an email and a password are required")

        cognito = remote.Cognito(http, pool["region"], pool["client_id"])
        outcome = await cognito.sign_in(email, password)
        if isinstance(outcome, remote.NewPasswordRequired):
            if not stdin.isatty():
                raise remote.RemoteError(
                    "this account must set a new password first: sign in once from a "
                    "terminal (or the web UI) to choose one"
                )
            _note(err, "| this is your first sign-in: choose a new password")
            new_password = getpass.getpass("New password: ", stream=err)
            if new_password != getpass.getpass("Repeat new password: ", stream=err):
                raise remote.RemoteError("the two passwords did not match")
            outcome = await cognito.set_new_password(email, new_password, outcome.session)

    remote.save_login(
        remote.Login(
            url=url,
            auth=pool,
            email=email,
            id_token=outcome.id_token,
            refresh_token=outcome.refresh_token,
            expires_at=outcome.expires_at,
        )
    )
    _note(err, f"| signed in to {url} as {email}; commands now run there")
    _note(err, "| pass --local to use this machine, or `nimbus logout` to stop")
    if args.json:
        _write(out, render.dumps({"url": url, "email": email}) + "\n")
    return EXIT_OK


async def logout(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Forget the saved login, revoking its refresh token first (best effort)."""
    saved = remote.load_login()
    if saved is None:
        _note(err, "| not signed in")
        return EXIT_OK
    if saved.auth is not None and saved.refresh_token:
        # Best effort: an unreachable pool must not stop someone signing out
        # of this machine, which is what the file deletion does.
        with contextlib.suppress(remote.RemoteError):
            async with remote.http_client() as http:
                await remote.cognito_for(http, saved).revoke(saved.refresh_token)
    remote.clear_login()
    _note(err, f"| signed out of {saved.url}; commands run on this machine again")
    return EXIT_OK


async def whoami(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Where commands run: which stack, and as whom. Offline."""
    saved = remote.load_login()
    if saved is None:
        raise remote.NotSignedInError(
            "not signed in: commands run on this machine (`nimbus login --url https://...` "
            "to use a stack)"
        )
    remaining = None if saved.expires_at is None else int(saved.expires_at - time.time())
    if args.json:
        _write(
            out,
            render.dumps({"url": saved.url, "email": saved.email, "expires_in": remaining}) + "\n",
        )
        return EXIT_OK
    who = saved.email or "(no sign-in required)"
    _write(out, f"{who} @ {saved.url}\n")
    if remaining is not None:
        state = f"valid for {remaining // 60} more minutes" if remaining > 0 else "expired"
        _note(err, f"| token {state}; it is refreshed automatically")
    _note(err, "| commands run on this stack; pass --local to use this machine")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Discovery: models, tools
# --------------------------------------------------------------------------- #


async def models(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """List every model reachable from this machine, across every provider.

    Same aggregation as ``GET /models``, including its degrade-to-empty
    behaviour: a provider that fails to list contributes nothing and does not
    fail the command.
    """
    target: remote.Login | None = getattr(args, "target", None)
    if target is not None:
        return await _models_on_stack(args, target, out, err)
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

    rows = _model_rows(result.models)
    if rows:
        _write(out, render.table(rows, ["PROVIDER", "MODEL ID", "NAME"]) + "\n")

    checks = diagnose.provider_checks(settings, result)
    for check in checks:
        if check.status == "fail":
            _note(err, f"| {check.name}: {check.detail}")
            _note(err, f"|   fix: {check.fix}")
    unconfigured = [check for check in checks if check.status == "off"]
    if unconfigured:
        listed = ", ".join(f"{check.name} ({check.fix})" for check in unconfigured)
        _note(err, f"| not configured: {listed}")
    if not rows:
        _note(err, "| no models available: `nimbus doctor` checks your setup")
    return EXIT_OK


def _model_rows(entries: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [entry.get("source", ""), entry.get("model_id", ""), entry.get("name", "")]
        for entry in entries
    ]


async def _models_on_stack(
    args: argparse.Namespace, login: remote.Login, out: TextIO, err: TextIO
) -> int:
    """The stack's ``GET /models``: what runs *there* can use, not what this machine can."""
    _on_stack(err, login)
    async with remote.http_client() as http:
        payload = await remote.RemoteApi(http, login).get("/models")
    if args.json:
        _write(out, render.dumps(payload) + "\n")
        return EXIT_OK
    rows = _model_rows(payload.get("models") or [])
    if rows:
        _write(out, render.table(rows, ["PROVIDER", "MODEL ID", "NAME"]) + "\n")
    unconfigured = [
        name
        for name, block in (payload.get("providers") or {}).items()
        if not (block or {}).get("configured")
    ]
    if unconfigured:
        _note(err, f"| not configured on the stack: {', '.join(unconfigured)}")
    if not rows:
        _note(err, "| the stack offers no models")
    return EXIT_OK


async def tools(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """List the toolsets a run may name with ``--toolset``."""
    target: remote.Login | None = getattr(args, "target", None)
    if target is not None:
        _on_stack(err, target)
        async with remote.http_client() as http:
            payload = await remote.RemoteApi(http, target).get("/tools")
        toolsets = payload.get("toolsets") or []
    else:
        toolsets = [{"name": name, "tools": names} for name, names in list_handlers().items()]
    if args.json:
        _write(out, render.dumps({"toolsets": toolsets}) + "\n")
        return EXIT_OK

    rows = [[entry["name"], ", ".join(entry["tools"])] for entry in toolsets]
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
    target: remote.Login | None = getattr(args, "target", None)
    if target is not None:
        return await _runs_on_stack(args, target, out, err)
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
    _run_table(rows, next_cursor, out, err)
    return EXIT_OK


def _run_table(rows: list[list[str]], next_cursor: str | None, out: TextIO, err: TextIO) -> None:
    if rows:
        _write(out, render.table(rows, ["ID", "WHEN", "STATUS", "MODEL"]) + "\n")
    else:
        _note(err, "| no runs stored")
    if next_cursor:
        _note(err, f"| more: --cursor {next_cursor}")


def _when(value: Any) -> str:
    """A server timestamp as the local table shows one: to the second."""
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


async def _runs_on_stack(
    args: argparse.Namespace, login: remote.Login, out: TextIO, err: TextIO
) -> int:
    _on_stack(err, login)
    async with remote.http_client() as http:
        page = await remote.RemoteApi(http, login).get(
            "/runs",
            {
                "limit": args.limit,
                "model_id": args.model,
                "status": args.status,
                "cursor": args.cursor,
            },
        )
    items, next_cursor = page.get("items") or [], page.get("next_cursor")
    if args.json:
        _write(out, render.dumps({"items": items, "next_cursor": next_cursor}) + "\n")
        return EXIT_OK
    rows = [
        [item["id"], _when(item.get("ts")), item.get("status", ""), item.get("model_id", "")]
        for item in items
    ]
    _run_table(rows, next_cursor, out, err)
    return EXIT_OK


async def show(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Print one stored run or evaluation as JSON.

    Runs and evaluations share an id namespace from the caller's point of view
    -- they have an id and they want to see it -- so this looks in both rather
    than making them remember which kind of thing they are holding.
    """
    target: remote.Login | None = getattr(args, "target", None)
    if target is not None:
        _on_stack(err, target)
        async with remote.http_client() as http:
            api = remote.RemoteApi(http, target)
            for path in (f"/runs/{args.id}", f"/evaluations/{args.id}"):
                with contextlib.suppress(remote.RemoteNotFoundError):
                    _write(out, render.dumps(await api.get(path)) + "\n")
                    return EXIT_OK
        raise NotFoundError(f"No run or evaluation with id {args.id!r} on {target.url}")

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
# Getting started: init, doctor
# --------------------------------------------------------------------------- #

#: The starter suite `init` writes -- byte for byte the documented example
#: (docs/examples/support-suite.yaml), which a test keeps true.
STARTER_SUITE = "starter-suite.yaml"
_STARTER_MODEL_LINE = "  model_id: amazon.nova-lite-v1:0\n"


def starter_suite(model: str | None = None) -> str:
    text = resources.files("nimbus.cli").joinpath(STARTER_SUITE).read_text(encoding="utf-8")
    if model:
        text = text.replace(_STARTER_MODEL_LINE, f"  model_id: {model}\n", 1)
    return text


async def init(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Write a commented starter suite to edit into your own; never over an existing file."""
    path = args.file
    if path.exists() and not args.force:
        raise AppError(f"{path} already exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(starter_suite(args.model), encoding="utf-8")
    if args.json:
        _write(out, render.dumps({"path": str(path)}) + "\n")
    _note(err, f"| wrote {path}: four example cases for a support prompt")
    _note(err, f"| run it:   nimbus eval --suite {path}")
    _note(err, "| models:   nimbus models   (then -m <model id>, or edit run_config.model_id)")
    return EXIT_OK


_DOCTOR_MARK = {"ok": "ok", "off": "--", "fail": "!!"}


async def doctor(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Check everything nimbus needs, and say what to do about what is missing.

    Exits ``1`` when something that is set up is broken, so it can gate a
    script; things that are simply not set up (a provider you do not use) are
    listed, not failures -- unless nothing at all can run a model.
    """
    from nimbus.cli.main import version

    catalog = await ProviderCatalog().collect(settings, ModelCatalog())
    local = diagnose.provider_checks(settings, catalog)
    login = remote.load_login()
    stack = await diagnose.stack_checks(login)
    checks = [diagnose.history_check(settings), *local, *stack]
    if not any(check.status == "ok" for check in (*local, *stack)):
        checks.append(
            diagnose.Check(
                "models",
                "fail",
                "nothing can run a model: no provider here, no stack",
                "set up one provider above, or sign in to a stack",
            )
        )
    healthy = not any(check.status == "fail" for check in checks)

    if args.json:
        _write(
            out,
            render.dumps(
                {
                    "version": version(),
                    "ok": healthy,
                    "checks": [
                        {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                        for c in checks
                    ],
                }
            )
            + "\n",
        )
    else:
        _write(out, f"nimbus {version()} (Python {platform.python_version()})\n\n")
        width = max(len(check.name) for check in checks)
        for check in checks:
            mark = _DOCTOR_MARK[check.status]
            _write(out, f"{mark}  {check.name.ljust(width)}  {check.detail}\n")
            if check.fix and check.status != "ok":
                _write(out, f"{' ' * (width + 6)}-> {check.fix}\n")
        problems = sum(1 for check in checks if check.status == "fail")
        _note(err, "| all good" if healthy else f"| {problems} problem{'s' * (problems != 1)}")
    return EXIT_OK if healthy else EXIT_FAILED


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


def serve(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Boot the HTTP API, which is what the web UI talks to.

    The UI is one way to use the harness, not the way, so starting it is a
    subcommand rather than a separate entry point people have to learn.

    **Deliberately not a coroutine.** ``uvicorn.run`` is the synchronous
    runner: it calls ``asyncio.run`` itself, and with ``--reload`` it forks a
    supervisor. Called from inside an already-running loop it raises
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``,
    so this is the one command the dispatcher must invoke directly rather than
    through ``asyncio.run`` -- which is why it is declared ``def``, not
    ``async def``, and why the dispatcher branches on that.
    """
    import uvicorn

    _note(err, f"| serving nimbus on http://{args.host}:{args.port}")
    uvicorn.run(
        "nimbus.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return EXIT_OK
