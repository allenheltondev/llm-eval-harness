"""``nimbus login`` / ``logout`` / ``whoami`` and ``eval --remote``.

The "remote" harness is the real evaluations API (``runs.router`` and the real
engine, with scripted models) served in-process over ``httpx.ASGITransport``,
behind a stand-in for the bearer check. Only Cognito is faked -- a handful of
``cognito-idp`` operations answered by :class:`FakeCognito` -- so what these
tests prove is the CLI's side of the contract: the requests it makes, the
tokens it keeps, and that an evaluation it starts is the server's, stored and
listed there like any other.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import time
from typing import Any, NamedTuple

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from sqlmodel import Session

from nimbus.cli import commands, remote
from nimbus.cli.main import main
from nimbus.errors import NotFoundError, register_exception_handlers
from nimbus.evals import engine as evals_engine
from nimbus.evals import jobs as evals_jobs
from nimbus.evals.judge import FakeJudgeModel, get_judge_factory
from nimbus.models_catalog import CatalogResult
from nimbus.routers import models as models_router
from nimbus.routers import runs
from nimbus.routers import tools as tools_router
from nimbus.store import db, history
from nimbus.store import db as store_db
from nimbus.store import repo as store_repo
from tests.test_cli import _Stdin
from tests.test_evals_router import STABLE_ANSWER, GatedModels, ScriptedModels

URL = "https://harness.example.com"
POOL_HOST = "cognito-idp.us-east-1.amazonaws.com"
EMAIL = "ada@example.com"
PASSWORD = "correct horse battery staple"


# --------------------------------------------------------------------------- #
# The fake world: a harness and a user pool
# --------------------------------------------------------------------------- #


class FakeCognito:
    """The user pool operations the CLI calls, answered from memory."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.issued = 0
        self.password = PASSWORD
        self.first_sign_in = False
        self.refresh_works = True
        self.expires_in = 3600

    def _tokens(self, *, with_refresh: bool) -> dict[str, Any]:
        self.issued += 1
        result: dict[str, Any] = {"IdToken": f"id-{self.issued}", "ExpiresIn": self.expires_in}
        if with_refresh:
            result["RefreshToken"] = "refresh-1"
        return {"AuthenticationResult": result}

    def handle(self, request: httpx.Request) -> httpx.Response:
        operation = request.headers["X-Amz-Target"].split(".")[-1]
        body = json.loads(request.content)
        self.calls.append((operation, body))
        if operation == "InitiateAuth" and body["AuthFlow"] == "USER_PASSWORD_AUTH":
            if body["AuthParameters"]["PASSWORD"] != self.password:
                return httpx.Response(400, json={"__type": "NotAuthorizedException"})
            if self.first_sign_in:
                return httpx.Response(
                    200, json={"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "s-1"}
                )
            return httpx.Response(200, json=self._tokens(with_refresh=True))
        if operation == "InitiateAuth" and body["AuthFlow"] == "REFRESH_TOKEN_AUTH":
            if not self.refresh_works:
                return httpx.Response(400, json={"__type": "NotAuthorizedException"})
            return httpx.Response(200, json=self._tokens(with_refresh=False))
        if operation == "RespondToAuthChallenge":
            return httpx.Response(200, json=self._tokens(with_refresh=True))
        if operation == "RevokeToken":
            return httpx.Response(200, json={})
        return httpx.Response(400, json={"__type": "UnsupportedOperation"})

    def operations(self) -> list[str]:
        return [f"{name}:{body.get('AuthFlow', '')}".rstrip(":") for name, body in self.calls]


class Harness:
    """What the fake server says about itself, and which tokens it accepts."""

    def __init__(self) -> None:
        self.auth_required = True
        self.cloud = False
        self.local = True
        self.valid_tokens: set[str] = set()
        #: Replaces fields of the published auth block (a hostile server).
        self.auth_overrides: dict[str, Any] = {}
        self.requests: list[tuple[str, str, str | None]] = []

    def health(self) -> dict[str, Any]:
        auth: dict[str, Any] = {"required": False}
        if self.auth_required:
            auth = {
                "required": True,
                "provider": "cognito",
                "region": "us-east-1",
                "user_pool_id": "us-east-1_pool",
                "client_id": "client-1",
                **self.auth_overrides,
            }
        return {
            "status": "ok",
            "cloud_evals": {"configured": self.cloud},
            "local_evals": {"available": self.local},
            "auth": auth,
        }


class StackCatalog:
    """What the stack's ``GET /models`` lists: one Bedrock model, nothing else set up."""

    async def collect(self, settings, bedrock) -> CatalogResult:
        return CatalogResult(
            models=[{"source": "bedrock", "model_id": "m1", "name": "Model One"}],
            providers={
                "bedrock": {"configured": True},
                "anthropic": {"configured": False},
                "openai": {"configured": False},
                "ollama": {"configured": False, "reachable": False},
            },
            cached=False,
        )


class Routed(httpx.AsyncBaseTransport):
    """Send user pool calls to the fake pool and everything else to the app."""

    def __init__(self, app: FastAPI, cognito: FakeCognito) -> None:
        self._app = httpx.ASGITransport(app=app)
        self._pool = httpx.MockTransport(cognito.handle)
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        if request.url.host == POOL_HOST:
            return await self._pool.handle_async_request(request)
        return await self._app.handle_async_request(request)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("NIMBUS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("EVALHARNESS_CONFIG_DIR", raising=False)
    monkeypatch.setattr(store_db, "_engine", None)
    store_repo.reset_cache()
    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(remote, "RECONNECT_BACKOFF_SECONDS", 0.0)
    evals_jobs.clear()
    db.init_db(str(tmp_path / "server.db"))
    yield
    evals_jobs.clear()


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def cognito() -> FakeCognito:
    return FakeCognito()


@pytest.fixture
def models() -> ScriptedModels:
    return ScriptedModels()


@pytest.fixture
def app(harness, models) -> FastAPI:
    def bearer(authorization: str | None = Header(default=None)) -> None:
        if not harness.auth_required:
            return
        token = (authorization or "").removeprefix("Bearer ")
        if token not in harness.valid_tokens:
            raise HTTPException(status_code=401, detail="invalid token")

    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return harness.health()

    application.include_router(runs.router, prefix="/api/v1", dependencies=[Depends(bearer)])
    for router in (models_router.router, tools_router.router):
        application.include_router(router, prefix="/api/v1", dependencies=[Depends(bearer)])
    application.dependency_overrides[models_router._get_provider_catalog] = StackCatalog
    application.dependency_overrides[runs.get_model_factory] = lambda: models
    judge = FakeJudgeModel()
    application.dependency_overrides[get_judge_factory] = lambda: lambda _id: judge
    return application


@pytest.fixture(autouse=True)
def wired(app, cognito, monkeypatch) -> Routed:
    routed = Routed(app, cognito)
    monkeypatch.setattr(remote, "transport", routed)
    return routed


class Result(NamedTuple):
    code: int
    out: str
    err: str


def run_cli(*argv: str, stdin: str = "", tty: bool = False) -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdin=_Stdin(stdin, tty=tty), stdout=out, stderr=err)
    return Result(code, out.getvalue(), err.getvalue())


def sign_in(harness: Harness, **overrides: Any) -> remote.Login:
    """Save a login as `nimbus login` would, and make the server accept it."""
    login = remote.Login(
        url=URL,
        auth={"region": "us-east-1", "client_id": "client-1"},
        email=EMAIL,
        id_token="id-0",
        refresh_token="refresh-1",
        expires_at=time.time() + 3600,
    )
    for name, value in overrides.items():
        setattr(login, name, value)
    remote.save_login(login)
    harness.valid_tokens.add(login.id_token or "")
    return login


def stored_evaluations() -> list[history.EvaluationRecord]:
    records, _ = store_repo.get_history_repo(_settings()).list_evaluations()
    return records


def _settings():
    from nimbus.config import Settings

    return Settings()


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #


class TestLogin:
    def test_signing_in_saves_a_login_only_its_owner_can_read(self, cognito):
        result = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )

        assert result.code == 0, result.err
        assert f"signed in to {URL} as {EMAIL}" in result.err
        saved = remote.load_login()
        assert saved is not None
        assert (saved.url, saved.email, saved.id_token) == (URL, EMAIL, "id-1")
        assert saved.refresh_token == "refresh-1"
        assert saved.auth == {"region": "us-east-1", "client_id": "client-1"}
        assert stat.S_IMODE(os.stat(remote.login_path()).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(remote.config_dir()).st_mode) == 0o700
        # The password went to the pool, and nowhere near the saved file.
        assert PASSWORD not in remote.login_path().read_text()
        assert cognito.calls[0][1]["AuthParameters"]["USERNAME"] == EMAIL

    def test_a_wrong_password_is_the_pools_sentence_and_saves_nothing(self):
        result = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin="wrong\n"
        )

        assert result.code == 1
        assert "Incorrect email or password." in result.err
        assert remote.load_login() is None

    def test_the_url_is_saved_as_the_origin(self):
        run_cli(
            "login",
            "--url",
            f"{URL}/api/v1/",
            "--email",
            EMAIL,
            "--password-stdin",
            stdin=PASSWORD + "\n",
        )

        assert remote.load_login().url == URL

    def test_the_next_login_reuses_the_saved_url_and_email(self, cognito):
        run_cli("login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n")

        result = run_cli("login", "--password-stdin", stdin=PASSWORD + "\n")

        assert result.code == 0, result.err
        assert cognito.calls[-1][1]["AuthParameters"]["USERNAME"] == EMAIL

    def test_a_server_without_sign_in_is_saved_without_tokens(self, harness, cognito):
        harness.auth_required = False

        result = run_cli("login", "--url", URL)

        assert result.code == 0, result.err
        saved = remote.load_login()
        assert saved.auth is None and saved.id_token is None and saved.signed_in
        assert cognito.calls == []

    @pytest.mark.parametrize(
        ("url", "problem"),
        [
            ("http://harness.example.com", "plain http"),
            ("ftp://harness.example.com", "not a server URL"),
            ("harness.example.com", "not a server URL"),
        ],
    )
    def test_urls_that_would_leak_or_mean_nothing_are_refused(self, url, problem):
        result = run_cli(
            "login", "--url", url, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )

        assert result.code == 1
        assert problem in result.err
        assert remote.load_login() is None

    def test_plain_http_is_fine_for_this_machine(self):
        assert remote.normalize_url("http://localhost:8000/") == "http://localhost:8000"
        assert remote.normalize_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"

    def test_something_that_is_not_a_harness_is_named_as_such(self, app):
        @app.get("/api/v1/other")
        def other() -> dict[str, str]:  # pragma: no cover - never called
            return {}

        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/health"
        ]

        result = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )

        assert result.code == 1
        assert "is not a Nimbus server" in result.err

    def test_without_a_terminal_the_password_must_come_on_stdin(self):
        result = run_cli("login", "--url", URL, "--email", EMAIL)

        assert result.code == 1
        assert "--password-stdin" in result.err

    def test_at_a_terminal_the_email_and_password_are_prompted_for(self, monkeypatch):
        monkeypatch.setattr(commands.getpass, "getpass", lambda prompt, stream=None: PASSWORD)

        result = run_cli("login", "--url", URL, stdin=EMAIL + "\n", tty=True)

        assert result.code == 0, result.err
        assert "Email: " in result.err
        assert remote.load_login().email == EMAIL

    def test_a_first_sign_in_sets_the_permanent_password(self, cognito, monkeypatch):
        cognito.first_sign_in = True
        answers = iter([PASSWORD, "new-password-1", "new-password-1"])
        monkeypatch.setattr(commands.getpass, "getpass", lambda prompt, stream=None: next(answers))

        result = run_cli("login", "--url", URL, "--email", EMAIL, tty=True)

        assert result.code == 0, result.err
        respond = [body for name, body in cognito.calls if name == "RespondToAuthChallenge"]
        assert respond[0]["ChallengeResponses"] == {
            "USERNAME": EMAIL,
            "NEW_PASSWORD": "new-password-1",
        }
        assert respond[0]["Session"] == "s-1"
        assert remote.load_login().id_token == "id-1"

    def test_mismatched_new_passwords_are_refused(self, cognito, monkeypatch):
        cognito.first_sign_in = True
        answers = iter([PASSWORD, "new-password-1", "new-password-2"])
        monkeypatch.setattr(commands.getpass, "getpass", lambda prompt, stream=None: next(answers))

        result = run_cli("login", "--url", URL, "--email", EMAIL, tty=True)

        assert result.code == 1
        assert "did not match" in result.err
        assert remote.load_login() is None

    def test_a_first_sign_in_needs_a_terminal(self, cognito):
        cognito.first_sign_in = True

        result = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )

        assert result.code == 1
        assert "must set a new password first" in result.err


class TestLogoutAndWhoami:
    def test_logout_revokes_the_refresh_token_and_forgets_the_login(self, harness, cognito):
        sign_in(harness)

        result = run_cli("logout")

        assert result.code == 0
        assert ("RevokeToken", {"Token": "refresh-1", "ClientId": "client-1"}) in cognito.calls
        assert remote.load_login() is None

    def test_logout_when_signed_out_is_not_an_error(self):
        assert run_cli("logout").code == 0

    def test_logout_still_forgets_the_login_when_the_pool_is_unreachable(
        self, harness, monkeypatch
    ):
        sign_in(harness)

        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(remote, "transport", httpx.MockTransport(unreachable))

        assert run_cli("logout").code == 0
        assert remote.load_login() is None

    def test_whoami_names_the_server_and_the_user(self, harness):
        sign_in(harness)

        result = run_cli("whoami")

        assert result.code == 0
        assert result.out == f"{EMAIL} @ {URL}\n"
        assert "valid for" in result.err

    def test_whoami_as_json(self, harness):
        sign_in(harness)

        payload = json.loads(run_cli("whoami", "--json").out)

        assert payload["url"] == URL and payload["email"] == EMAIL
        assert 0 < payload["expires_in"] <= 3600

    def test_whoami_signed_out_is_a_failure(self):
        result = run_cli("whoami")

        assert result.code == 1
        assert "not signed in" in result.err


# --------------------------------------------------------------------------- #
# eval --remote
# --------------------------------------------------------------------------- #


class TestRemoteEval:
    def test_the_evaluation_runs_on_the_server_and_is_stored_there(self, harness, models):
        sign_in(harness)

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "Assess order B456", "-n", "3")

        assert result.code == 0, result.err
        terminal = json.loads(result.out)
        assert terminal["status"] == "completed"
        assert terminal["result"]["grade"] == "A"
        assert len(terminal["run_ids"]) == 3
        assert terminal["url"] == f"{URL}/#/evals/{terminal['evaluation_id']}"
        assert models.call_count == 3
        # Stored by the *server*, marked as the CLI's, where its UI lists it.
        [stored] = stored_evaluations()
        assert stored.id == terminal["evaluation_id"]
        assert stored.config["source"] == "cli"
        assert stored.status == "completed"
        # The progress and the link went to stderr.
        assert terminal["url"] in result.err
        assert "grade A" in result.err

    def test_json_mode_streams_the_servers_events(self, harness):
        sign_in(harness)

        result = run_cli("eval", "--remote", "--json", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err
        lines = [json.loads(line) for line in result.out.splitlines()]
        assert lines[0]["type"] == "eval_start"
        assert lines[-1]["type"] == "eval_complete"
        assert lines[-1]["status"] == "completed"

    def test_detach_submits_and_returns(self, harness, models):
        sign_in(harness)

        result = run_cli("eval", "--remote", "--detach", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err
        payload = json.loads(result.out)
        assert payload["url"] == f"{URL}/#/evals/{payload['evaluation_id']}"
        assert [record.id for record in stored_evaluations()] == [payload["evaluation_id"]]

    def test_a_suite_runs_remotely(self, harness, tmp_path):
        sign_in(harness)
        suite = tmp_path / "cases.yaml"
        suite.write_text(
            "name: smoke\nrun_config:\n  model_id: m1\ncases:\n  - id: a\n    input: hi\n",
            encoding="utf-8",
        )

        result = run_cli("eval", "--remote", "--suite", str(suite))

        assert result.code == 0, result.err
        [stored] = stored_evaluations()
        assert stored.kind == "suite"
        assert stored.config["suite"]["name"] == "smoke"

    def test_an_expired_token_is_refreshed_first_and_saved(self, harness, cognito):
        sign_in(harness, expires_at=time.time() - 5)
        harness.valid_tokens.add("id-1")

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err
        assert cognito.operations() == ["InitiateAuth:REFRESH_TOKEN_AUTH"]
        saved = remote.load_login()
        assert saved.id_token == "id-1"
        assert saved.refresh_token == "refresh-1"  # a refresh does not rotate it
        assert saved.expires_at > time.time()

    def test_a_token_the_server_rejects_is_refreshed_once_and_retried(self, harness, cognito):
        sign_in(harness)
        harness.valid_tokens = {"id-1"}  # the saved id-0 is revoked server-side

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err
        assert cognito.operations().count("InitiateAuth:REFRESH_TOKEN_AUTH") == 1

    def test_a_refresh_that_fails_asks_for_a_new_login(self, harness, cognito):
        sign_in(harness, expires_at=time.time() - 5)
        cognito.refresh_works = False

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 1
        assert "has expired: run `nimbus login`" in result.err
        assert stored_evaluations() == []

    def test_signed_out_is_a_failure_with_directions(self):
        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 1
        assert "nimbus login --url" in result.err

    def test_the_servers_own_error_is_what_the_user_reads(self, harness):
        sign_in(harness)

        result = run_cli("eval", "--remote", "--run", "no-such-run")

        assert result.code == 1
        assert "no-such-run" in result.err

    def test_a_server_with_no_lane_says_so(self, harness):
        sign_in(harness)
        harness.local = False

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 1
        assert "has no evaluation lane" in result.err

    def test_a_server_without_sign_in_needs_no_token(self, harness):
        harness.auth_required = False
        remote.save_login(remote.Login(url=URL))

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err

    def test_remote_and_db_do_not_mix(self, tmp_path):
        result = run_cli("--db", str(tmp_path / "x.db"), "eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 2
        assert "--db is local only" in result.err

    async def test_ctrl_c_cancels_the_evaluation_on_the_server(self, harness, app):
        sign_in(harness)
        gated = GatedModels()
        app.dependency_overrides[runs.get_model_factory] = lambda: gated
        args = _eval_args("eval", "--remote", "-m", "m1", "-p", "hi", "-n", "3")

        task = asyncio.create_task(
            commands.evaluate(args, _settings(), io.StringIO(), io.StringIO())
        )
        await asyncio.wait_for(gated.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        [stored] = stored_evaluations()
        assert stored.status == "cancelled"


def _eval_args(*argv: str):
    from nimbus.cli.main import build_parser, prepare

    args = build_parser().parse_args(list(argv))
    prepare(args, _Stdin("", tty=True))
    return args


def test_the_cloud_lane_is_preferred_when_the_server_has_it():
    assert commands._choose_lane({"cloud_evals": {"configured": True}}, URL) == "cloud"
    assert (
        commands._choose_lane(
            {"cloud_evals": {"configured": False}, "local_evals": {"available": True}}, URL
        )
        == "local"
    )


# --------------------------------------------------------------------------- #
# The event stream, reconnecting
# --------------------------------------------------------------------------- #


def _lines(*entries: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(entry).encode() + b"\n" for entry in entries)


RUN_1 = {"type": "run_complete", "index": 0}
RUN_2 = {"type": "run_complete", "index": 1}
DONE = {"type": "eval_complete", "status": "completed", "result": None}


async def _follow(
    responses: list[Any], requests: list[httpx.Request] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Follow one evaluation against a scripted sequence of stream responses."""
    served = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        if requests is not None:
            requests.append(request)
        reply = responses[min(served, len(responses) - 1)]
        served += 1
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "Evaluation 'e' not found"}})
        return httpx.Response(200, content=reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = remote.RemoteApi(http, remote.Login(url=URL))
        seen = [entry async for entry in api.events("e")]
    return seen, served


async def test_a_dropped_stream_resumes_without_repeating_events():
    seen, served = await _follow([_lines(RUN_1), _lines(RUN_1, RUN_2, DONE)])

    assert seen == [RUN_1, RUN_2, DONE]
    assert served == 2


async def test_a_broken_connection_is_retried():
    seen, _ = await _follow([httpx.ReadError("reset"), _lines(RUN_1, DONE)])

    assert seen == [RUN_1, DONE]


async def test_a_replayed_log_that_is_only_the_ending_still_ends_it():
    # A server that lost the job's log replays eval_complete alone.
    seen, _ = await _follow([_lines(RUN_1, RUN_2), _lines(DONE)])

    assert seen == [RUN_1, RUN_2, DONE]


async def test_a_stream_that_never_finishes_gives_up_with_the_link():
    with pytest.raises(remote.RemoteError, match=r"lost the event stream.*#/evals/e"):
        await _follow([_lines(RUN_1)])


async def test_a_server_that_answers_no_is_not_retried():
    requests: list[httpx.Request] = []

    with pytest.raises(remote.RemoteError, match="Evaluation 'e' not found") as raised:
        await _follow([404], requests)

    assert len(requests) == 1
    assert "lost the event stream" not in str(raised.value)


def test_a_corrupt_login_file_reads_as_signed_out():
    remote.config_dir().mkdir(parents=True)
    remote.login_path().write_text("{not json", encoding="utf-8")

    assert remote.load_login() is None


# --------------------------------------------------------------------------- #
# The unhappy paths, as the user reads them
# --------------------------------------------------------------------------- #


class TestWhatGoesWrong:
    def test_login_without_any_url_asks_for_one(self):
        result = run_cli("login", "--password-stdin", stdin=PASSWORD + "\n")

        assert result.code == 1
        assert "pass --url" in result.err

    def test_login_without_a_terminal_needs_an_email(self):
        result = run_cli("login", "--url", URL, "--password-stdin", stdin=PASSWORD + "\n")

        assert result.code == 1
        assert "pass --email" in result.err

    def test_an_empty_password_is_refused_before_the_pool_is_asked(self, cognito):
        result = run_cli("login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin="\n")

        assert result.code == 1
        assert "an email and a password are required" in result.err
        assert cognito.calls == []

    def test_login_as_json_names_the_server_and_user(self):
        result = run_cli(
            "login",
            "--json",
            "--url",
            URL,
            "--email",
            EMAIL,
            "--password-stdin",
            stdin=PASSWORD + "\n",
        )

        assert json.loads(result.out) == {"url": URL, "email": EMAIL}

    def test_a_server_without_sign_in_as_json(self, harness):
        harness.auth_required = False

        result = run_cli("login", "--json", "--url", URL)

        assert json.loads(result.out) == {"url": URL, "email": None}

    def test_an_unreachable_server_is_named(self, monkeypatch):
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(remote, "transport", httpx.MockTransport(unreachable))

        result = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )

        assert result.code == 1
        assert f"could not reach {URL}" in result.err

    @pytest.mark.parametrize(
        ("reply", "reason"),
        [
            (httpx.Response(404, json={"error": {"message": "Not Found"}}), "Not Found"),
            (httpx.Response(200, content=b"<html>"), "is not JSON"),
            (httpx.Response(200, json={"status": "ok"}), "has no auth block"),
            (httpx.Response(502, content=b"bad gateway"), "HTTP 502"),
        ],
    )
    def test_something_that_is_not_a_harness_says_why(self, monkeypatch, reply, reason):
        monkeypatch.setattr(remote, "transport", httpx.MockTransport(lambda request: reply))

        result = run_cli("login", "--url", URL)

        assert result.code == 1
        assert "is not a Nimbus server" in result.err
        assert reason in result.err

    def test_an_unreachable_user_pool_is_named(self, app, monkeypatch):
        inner = httpx.ASGITransport(app=app)

        class PoolDown(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.host == POOL_HOST:
                    raise httpx.ConnectError("pool down")
                return await inner.handle_async_request(request)

        monkeypatch.setattr(remote, "transport", PoolDown())

        result = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )

        assert result.code == 1
        assert "could not reach the sign-in service" in result.err

    async def test_a_challenge_the_cli_cannot_answer_is_named(self):
        def mfa(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "s"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(mfa)) as http:
            with pytest.raises(remote.RemoteError, match="SOFTWARE_TOKEN_MFA"):
                await remote.Cognito(http, "us-east-1", "c").sign_in(EMAIL, PASSWORD)

    def test_a_login_without_a_refresh_token_asks_to_sign_in_again(self, harness):
        sign_in(harness, expires_at=time.time() - 5, refresh_token=None)

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 1
        assert "has expired: run `nimbus login`" in result.err

    def test_a_token_refused_even_after_a_refresh_is_reported(self, harness):
        sign_in(harness)
        harness.valid_tokens = set()  # nothing this user holds is accepted

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 1
        assert "refused the sign-in" in result.err

    def test_a_login_file_with_no_token_is_not_signed_in(self, harness):
        remote.save_login(remote.Login(url=URL, auth={"region": "us-east-1", "client_id": "c"}))

        result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 1
        assert "not signed in" in result.err

    def test_logout_of_a_server_without_sign_in_just_forgets_it(self, cognito):
        remote.save_login(remote.Login(url=URL))

        assert run_cli("logout").code == 0
        assert remote.load_login() is None
        assert cognito.calls == []

    def test_whoami_for_a_server_without_sign_in(self):
        remote.save_login(remote.Login(url=URL))

        result = run_cli("whoami")

        assert result.out == f"(no sign-in required) @ {URL}\n"

    def test_whoami_with_an_expired_token_says_so(self, harness):
        sign_in(harness, expires_at=time.time() - 60)

        assert "token expired" in run_cli("whoami").err


def test_the_config_directory_follows_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("NIMBUS_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert remote.config_dir() == tmp_path / "xdg" / "nimbus"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert remote.config_dir() == tmp_path / "home" / ".config" / "nimbus"


def test_clearing_a_login_that_is_not_there_says_so():
    assert remote.clear_login() is False


# --------------------------------------------------------------------------- #
# A server that lies about its user pool gets no password
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        # Would build https://cognito-idp.us-east-1.amazonaws.com.evil.com#.amazonaws.com/
        {"region": "us-east-1.amazonaws.com.evil.com#"},
        {"region": "evil.com/", "user_pool_id": "evil.com/_x"},
        {"region": "us-east-1@evil.com"},
        # A well-formed region that is not the pool's own.
        {"region": "eu-west-1"},
        {"user_pool_id": None},
        {"client_id": "a b/c"},
        {"region": None},
    ],
)
def test_a_pool_that_is_not_a_cognito_pool_is_refused_before_the_password(
    harness, cognito, wired, monkeypatch, overrides
):
    harness.auth_overrides = overrides

    def no_password(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("prompted for a password")

    monkeypatch.setattr(commands.getpass, "getpass", no_password)

    result = run_cli("login", "--url", URL, "--email", EMAIL, tty=True)

    assert result.code == 1
    assert "refusing to send a password" in result.err
    assert cognito.calls == []
    assert set(wired.hosts) == {"harness.example.com"}
    assert remote.load_login() is None


@pytest.mark.parametrize("region", ["us-gov-west-1", "cn-northwest-1", "ap-southeast-4"])
def test_every_shape_of_aws_region_is_accepted(region):
    pool = {"region": region, "client_id": "abc123", "user_pool_id": f"{region}_Abc"}

    assert remote.checked_pool(pool, URL) == {"region": region, "client_id": "abc123"}


def test_a_tampered_login_file_cannot_redirect_a_refresh(harness, cognito, wired):
    sign_in(
        harness,
        expires_at=time.time() - 5,
        auth={"region": "us-east-1.amazonaws.com.evil.com#", "client_id": "client-1"},
    )

    result = run_cli("eval", "--remote", "-m", "m1", "-p", "hi")

    assert result.code == 1
    assert "run `nimbus login`" in result.err
    # The refresh token went nowhere: not to the pool, not to the injected host.
    assert cognito.calls == []
    assert all("evil" not in host for host in wired.hosts)


def test_the_user_pool_client_itself_refuses_a_bad_region():
    with pytest.raises(remote.RemoteError, match="names no valid user pool"):
        remote.Cognito(httpx.AsyncClient(), "us-east-1.evil.com#", "client-1")


def test_concurrent_saves_never_share_a_temporary_file():
    """Two `eval --remote` refreshing at once: every read sees a whole login."""
    import threading

    errors: list[BaseException] = []

    def save(n: int) -> None:
        try:
            for i in range(50):
                remote.save_login(remote.Login(url=URL, email=f"user{n}-{i}@example.com"))
                if remote.load_login() is None:
                    raise AssertionError("read a torn login file")
        except BaseException as exc:  # a thread's failure must fail the test
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert remote.load_login().url == URL
    assert stat.S_IMODE(os.stat(remote.login_path()).st_mode) == 0o600
    # No temporary file is left behind.
    assert [path.name for path in remote.config_dir().iterdir()] == ["login.json"]


def test_a_save_that_fails_leaves_no_temporary_file(monkeypatch):
    remote.save_login(remote.Login(url=URL, email="before@example.com"))

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(remote.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        remote.save_login(remote.Login(url=URL, email="after@example.com"))

    assert remote.load_login().email == "before@example.com"
    assert [path.name for path in remote.config_dir().iterdir()] == ["login.json"]


# --------------------------------------------------------------------------- #
# The signed-in stack is where commands run
# --------------------------------------------------------------------------- #


def stack_hosts(wired: Routed) -> list[str]:
    return [host for host in wired.hosts if host != POOL_HOST]


class TestTarget:
    def test_a_run_goes_to_the_stack_once_signed_in(self, harness, models, wired):
        sign_in(harness)

        result = run_cli("run", "-m", "m1", "-p", "Assess order B456")

        assert result.code == 0, result.err
        assert result.out == STABLE_ANSWER + "\n"
        assert models.call_count == 1  # the stack's model, not one built here
        assert f"| on {URL} as {EMAIL}" in result.err
        assert "completed  run=" in result.err
        assert stack_hosts(wired)

    def test_a_run_on_the_stack_streams_its_events_as_json(self, harness):
        sign_in(harness)

        result = run_cli("run", "--json", "-m", "m1", "-p", "hi")

        assert result.code == 0, result.err
        types = [json.loads(line)["type"] for line in result.out.splitlines()]
        assert types[0] == "run_start"
        assert types[-1] == "run_complete"
        assert "text_delta" in types

    def test_event_types_this_cli_does_not_know_are_skipped(self, harness, monkeypatch):
        sign_in(harness)

        async def newer_server(self, body):
            yield {
                "type": "run_start",
                "run_id": "r1",
                "model_id": "m1",
                "ts": "2026-09-24T10:00:00Z",
            }
            yield {"type": "something_new", "detail": 1}
            yield {"type": "text_delta", "text": "hello"}
            yield {"type": "run_complete", "run_id": "r1", "status": "completed"}

        monkeypatch.setattr(remote.RemoteApi, "run", newer_server)

        result = run_cli("run", "-m", "m1", "-p", "hi")

        assert result.code == 0, result.err
        assert result.out == "hello\n"

    def test_the_stacks_refusal_of_a_run_is_what_the_user_reads(self, harness):
        sign_in(harness)

        result = run_cli("run", "-m", "m1", "-p", "hi", "--toolset", "no-such-toolset")

        assert result.code == 1
        assert "no-such-toolset" in result.err

    def test_an_evaluation_goes_to_the_stack_without_asking(self, harness, models):
        sign_in(harness)

        result = run_cli("eval", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err
        assert f"submitted to {URL}" in result.err
        [stored] = stored_evaluations()
        assert stored.id == json.loads(result.out)["evaluation_id"]

    def test_detach_needs_no_remote_flag_on_a_stack(self, harness):
        sign_in(harness)

        result = run_cli("eval", "--detach", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 0, result.err
        assert json.loads(result.out)["url"].startswith(f"{URL}/#/evals/")

    def test_detach_on_this_machine_is_a_usage_error(self, wired):
        result = run_cli("eval", "--detach", "-m", "m1", "-p", "hi", "-n", "2")

        assert result.code == 2
        assert "--detach needs a stack" in result.err
        assert wired.hosts == []

    def test_local_uses_this_machine_even_when_signed_in(self, harness, wired):
        sign_in(harness)

        result = run_cli("runs", "--local")

        assert result.code == 0, result.err
        assert stack_hosts(wired) == []
        assert "| on " not in result.err

    def test_a_db_file_means_this_machine(self, harness, wired, tmp_path):
        sign_in(harness)

        result = run_cli("--db", str(tmp_path / "scratch.db"), "runs")

        assert result.code == 0, result.err
        assert stack_hosts(wired) == []

    def test_local_and_remote_together_is_a_usage_error(self, harness):
        sign_in(harness)

        result = run_cli("eval", "--local", "--remote", "-m", "m1", "-p", "hi")

        assert result.code == 2
        assert "contradict" in result.err

    def test_runs_lists_the_stacks_history(self, harness):
        sign_in(harness)
        run_id = json.loads(run_cli("run", "--json", "-m", "m1", "-p", "hi").out.splitlines()[0])[
            "run_id"
        ]

        table = run_cli("runs")
        page = run_cli("runs", "--json", "--limit", "5")

        assert table.code == 0, table.err
        assert run_id in table.out
        assert table.out.splitlines()[0].split() == ["ID", "WHEN", "STATUS", "MODEL"]
        assert f"| on {URL}" in table.err
        assert [item["id"] for item in json.loads(page.out)["items"]] == [run_id]

    def test_runs_passes_its_filters_to_the_stack(self, harness):
        sign_in(harness)
        run_cli("run", "-m", "m1", "-p", "hi")

        result = run_cli("runs", "--json", "--model", "someone-else")

        assert json.loads(result.out)["items"] == []

    def test_show_finds_a_run_or_an_evaluation_on_the_stack(self, harness):
        sign_in(harness)
        evaluation = json.loads(run_cli("eval", "-m", "m1", "-p", "hi", "-n", "2").out)

        by_run = json.loads(run_cli("show", evaluation["run_ids"][0]).out)
        by_evaluation = json.loads(run_cli("show", evaluation["evaluation_id"]).out)

        assert by_run["id"] == evaluation["run_ids"][0]
        assert by_evaluation["id"] == evaluation["evaluation_id"]
        assert by_evaluation["source"] == "cli"

    def test_show_names_the_stack_when_nothing_matches(self, harness):
        sign_in(harness)

        result = run_cli("show", "nope")

        assert result.code == 1
        assert f"No run or evaluation with id 'nope' on {URL}" in result.err

    def test_models_are_the_stacks(self, harness):
        sign_in(harness)

        table = run_cli("models")
        payload = json.loads(run_cli("models", "--json").out)

        assert table.code == 0, table.err
        assert "m1" in table.out and "Model One" in table.out
        assert "not configured on the stack: anthropic, openai, ollama" in table.err
        assert payload["models"][0]["model_id"] == "m1"

    def test_a_stack_with_no_models_says_so(self, harness, app):
        sign_in(harness)

        class Empty:
            async def collect(self, settings, bedrock):
                return CatalogResult(models=[], providers={}, cached=False)

        app.dependency_overrides[models_router._get_provider_catalog] = Empty

        result = run_cli("models")

        assert result.out == ""
        assert "the stack offers no models" in result.err

    def test_tools_are_the_stacks(self, harness):
        sign_in(harness)

        table = run_cli("tools")
        payload = json.loads(run_cli("tools", "--json").out)

        assert table.code == 0, table.err
        assert table.out.splitlines()[0].split() == ["TOOLSET", "TOOLS"]
        assert payload["toolsets"]
        assert f"| on {URL}" in table.err

    def test_signing_in_and_out_says_where_commands_run(self, harness, cognito):
        signed_in = run_cli(
            "login", "--url", URL, "--email", EMAIL, "--password-stdin", stdin=PASSWORD + "\n"
        )
        whoami = run_cli("whoami")
        signed_out = run_cli("logout")

        assert "commands now run there" in signed_in.err
        assert "--local" in signed_in.err
        assert "commands run on this stack" in whoami.err
        assert "commands run on this machine again" in signed_out.err


class TestDoctorOnAStack:
    @pytest.fixture(autouse=True)
    def nothing_local(self, monkeypatch):
        """This machine has no provider: the stack is what doctor has to find."""

        async def collect(self, settings, bedrock):
            return CatalogResult(models=[], providers={}, cached=False)

        monkeypatch.setattr(commands.ProviderCatalog, "collect", collect)
        monkeypatch.setattr(
            commands.diagnose.providers, "aws_credentials_status", lambda: "missing"
        )

    def test_a_working_stack_is_enough(self, harness):
        sign_in(harness)

        result = run_cli("doctor")

        assert result.code == 0, result.out
        assert f"ok  stack      {URL} as {EMAIL}: server evaluations, 1 model" in result.out

    def test_the_cloud_lane_is_named(self, harness):
        harness.cloud = True
        sign_in(harness)

        assert "cloud evaluations" in run_cli("doctor").out

    def test_a_sign_in_the_stack_no_longer_accepts(self, harness, cognito):
        sign_in(harness)
        harness.valid_tokens.clear()
        cognito.refresh_works = False

        result = run_cli("doctor")

        assert result.code == 1
        assert "!!  stack" in result.out
        assert "-> `nimbus login` to sign in again" in result.out

    def test_an_unreachable_stack(self, harness, monkeypatch):
        sign_in(harness)

        def refuse(request):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(remote, "transport", httpx.MockTransport(refuse))

        result = run_cli("doctor")

        assert result.code == 1
        assert f"-> check that {URL} is up" in result.out

    def test_a_stack_without_models(self, harness, app):
        sign_in(harness)

        class Empty:
            async def collect(self, settings, bedrock):
                return CatalogResult(models=[], providers={}, cached=False)

        app.dependency_overrides[models_router._get_provider_catalog] = Empty

        result = run_cli("doctor")

        assert result.code == 1
        assert "needs Bedrock model access" in result.out


def test_a_run_whose_stream_breaks_is_reported(harness, monkeypatch):
    sign_in(harness)

    class Breaks(httpx.AsyncByteStream):
        async def __aiter__(self):
            start = {"type": "run_start", "run_id": "r1", "model_id": "m1", "ts": "2026-09-24"}
            yield (json.dumps(start) + "\n").encode()
            raise httpx.ReadError("connection reset")

    def reply(request):
        return httpx.Response(200, stream=Breaks())

    monkeypatch.setattr(remote, "transport", httpx.MockTransport(reply))

    result = run_cli("run", "-m", "m1", "-p", "hi")

    assert result.code == 1
    assert f"lost the run's stream from {URL}: connection reset" in result.err


class TestGradingRunsFromThisMachine:
    """``eval --run`` on a stack that never saw those runs grades them here instead."""

    @pytest.fixture
    def local_only(self, app, monkeypatch):
        """Run ids the stack does not have (they live in this machine's history)."""
        hidden: set[str] = set()

        class StackRepo:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def get_run(self, run_id):
                if run_id in hidden:
                    raise NotFoundError(f"Run {run_id!r} not found")
                return self._inner.get_run(run_id)

        app.dependency_overrides[runs.get_repo] = lambda: StackRepo(
            store_repo.get_history_repo(_settings())
        )
        # The local judge: this machine grades with the fake model, never AWS.
        monkeypatch.setenv("NIMBUS_FAKE_MODEL", "1")
        return hidden

    def local_run(self, hidden: set[str]) -> str:
        with Session(db.get_engine()) as session:
            record = history.create_run(
                session,
                model_id="m1",
                system_prompt="",
                user_prompt="hi",
                output="An answer.",
                status="completed",
            )
        hidden.add(record.id)
        return record.id

    def test_runs_the_stack_does_not_have_are_graded_here(self, harness, local_only):
        sign_in(harness)
        run_id = self.local_run(local_only)

        result = run_cli("eval", "--run", run_id)

        assert result.code == 0, result.err
        assert f"| Run {run_id!r} not found on {URL}; grading on this machine instead" in result.err
        terminal = json.loads(result.out)
        assert terminal["status"] == "completed"
        assert "url" not in terminal  # graded here: there is no page on the stack
        [stored] = stored_evaluations()
        assert stored.kind == "grade"
        assert stored.run_ids == [run_id]

    def test_runs_on_the_stack_are_graded_there(self, harness, local_only):
        sign_in(harness)
        run_id = json.loads(run_cli("run", "--json", "-m", "m1", "-p", "hi").out.splitlines()[0])[
            "run_id"
        ]

        result = run_cli("eval", "--run", run_id)

        assert result.code == 0, result.err
        assert "grading on this machine" not in result.err
        assert json.loads(result.out)["url"].startswith(f"{URL}/#/evals/")

    def test_remote_insists_on_the_stack(self, harness, local_only):
        sign_in(harness)
        run_id = self.local_run(local_only)

        result = run_cli("eval", "--remote", "--run", run_id)

        assert result.code == 1
        assert f"Run {run_id!r} not found" in result.err
        assert "grading on this machine" not in result.err
        assert stored_evaluations() == []

    def test_a_run_nowhere_is_still_an_error(self, harness, local_only):
        sign_in(harness)
        local_only.add("nope")

        result = run_cli("eval", "--run", "nope")

        assert result.code == 1
        assert "grading on this machine instead" in result.err
        assert result.err.rstrip().endswith("nimbus: Run 'nope' not found")
