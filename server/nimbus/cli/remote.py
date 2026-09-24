"""Driving a deployed stack from the CLI: sign-in, the saved login, and the API.

``nimbus login --url URL`` signs in the way the web UI does -- the server's
``GET /health`` names the Cognito pool, and the user pool's own API takes an
email and password (``USER_PASSWORD_AUTH``; there is no Hosted UI) -- and saves
the resulting tokens. From then on commands go through that server's API:
``nimbus eval`` submits to ``POST /evaluations`` and follows
``GET /evaluations/{id}/events``, which is byte-for-byte the NDJSON the local
command renders. The evaluation is
the server's, stored in the server's history, so the UI lists it alongside the
ones it started itself.

The saved login is a bearer credential. It lives in the user's config directory
(``$NIMBUS_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/nimbus``, else
``~/.config/nimbus``), readable only by its owner, and it is only ever sent
to the server it was issued for -- and never over plain HTTP to anything but
this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from nimbus.errors import AppError

#: The API is mounted here on every server; the SPA is served from the root of
#: the same origin, which is what makes one URL enough for both.
API_PREFIX = "/api/v1"

LOGIN_FILE_NAME = "login.json"

#: Refresh the ID token when it has less than this long to live, so a request
#: never races an expiry in flight.
REFRESH_MARGIN_SECONDS = 60

#: How many times a dropped event stream is re-opened before giving up. Each
#: reconnect replays the log from the start, so nothing is lost -- only
#: skipped -- and a deployed API's own request timeout is the usual reason a
#: long evaluation's stream ends early.
MAX_RECONNECTS = 10
RECONNECT_BACKOFF_SECONDS = 1.0

#: An AWS region name (``us-east-1``, ``eu-central-2``, ``us-gov-west-1``,
#: ``cn-northwest-1``). The user pool's endpoint is built from it, and it comes
#: from the server's ``/health`` -- so anything else (``us-east-1.evil.com#``)
#: would point the password at a host of the server's choosing.
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")
#: A user pool id is ``<region>_<id>``: the cross-check that the region is the pool's.
_USER_POOL_ID = re.compile(r"(?P<region>[a-z]{2}(?:-[a-z]+)+-\d{1,2})_[A-Za-z0-9]+")
#: App client ids are short alphanumeric strings (sent in the body, never the URL).
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: Hosts a login may be saved for over plain HTTP: this machine only.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Cognito exception name -> the sentence a person should read. Mirrors the
#: web UI's copy (app/src/auth/core.ts) so both front doors say the same thing.
_COGNITO_MESSAGES = {
    "NotAuthorizedException": "Incorrect email or password.",
    "UserNotFoundException": "Incorrect email or password.",
    "InvalidPasswordException": "That password doesn't meet the requirements.",
    "InvalidParameterException": "Please check what you entered and try again.",
    "LimitExceededException": "Too many attempts -- wait a few minutes and try again.",
    "TooManyRequestsException": "Too many attempts -- wait a moment and try again.",
    "PasswordResetRequiredException": "Your password must be reset before you can sign in.",
    "UserNotConfirmedException": "This account hasn't verified its email yet.",
}


class RemoteError(AppError):
    """Anything that stops the CLI talking to a remote harness. Exits 1."""

    code = "remote_error"


class NotSignedInError(RemoteError):
    code = "not_signed_in"


class RemoteNotFoundError(RemoteError):
    """The server answered 404: the thing asked for is not there."""

    code = "not_found"


class UnreachableError(RemoteError):
    """The server could not be reached at all (as opposed to answering "no")."""

    code = "unreachable"


# --------------------------------------------------------------------------- #
# The saved login
# --------------------------------------------------------------------------- #


@dataclass
class Login:
    """Where to send requests, and (for a server that wants one) as whom."""

    url: str
    #: ``{"region": ..., "client_id": ...}`` for a Cognito-protected server;
    #: ``None`` for one with sign-in off (a laptop's ``nimbus serve``).
    auth: dict[str, str] | None = None
    email: str | None = None
    id_token: str | None = None
    refresh_token: str | None = None
    #: Epoch seconds at which ``id_token`` stops being accepted.
    expires_at: float | None = None

    @property
    def signed_in(self) -> bool:
        return self.auth is None or bool(self.id_token)


#: The directory name before the rename, under the same config home.
LEGACY_CONFIG_DIR_NAME = "evalharness"


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def config_dir() -> Path:
    """Where the login is kept (see the module docstring for the order).

    ``$EVALHARNESS_CONFIG_DIR``, the name from before the rename, is still
    honoured when ``$NIMBUS_CONFIG_DIR`` is not set.
    """
    explicit = os.environ.get("NIMBUS_CONFIG_DIR") or os.environ.get("EVALHARNESS_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    return _config_home() / "nimbus"


def login_path() -> Path:
    return config_dir() / LOGIN_FILE_NAME


def _legacy_login_path() -> Path | None:
    """Where a login saved before the rename is, when it could be anywhere else.

    An explicit config directory is taken as given: nothing is looked for (or
    moved) outside it.
    """
    if os.environ.get("NIMBUS_CONFIG_DIR") or os.environ.get("EVALHARNESS_CONFIG_DIR"):
        return None
    return _config_home() / LEGACY_CONFIG_DIR_NAME / LOGIN_FILE_NAME


def _read_login(path: Path) -> Login | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Login(**data)
    except (OSError, ValueError, TypeError):
        return None


def load_login() -> Login | None:
    """The saved login, or ``None`` when there is none (or it is unreadable).

    A login saved under the pre-rename directory is moved to the current one
    the first time it is read, so ``logout`` has one file to forget.
    """
    path = login_path()
    if path.exists():
        return _read_login(path)
    legacy = _legacy_login_path()
    if legacy is None or not legacy.exists():
        return None
    login = _read_login(legacy)
    if login is not None:
        save_login(login)
        with contextlib.suppress(OSError):
            legacy.unlink()
    return login


def save_login(login: Login) -> None:
    """Write the login readable by its owner alone.

    Written to a temporary file of its own -- ``mkstemp`` creates it ``0600``
    with a unique name -- and moved into place with ``os.replace``. Unique
    matters: two ``eval --remote`` commands refreshing at once must not share
    (and truncate) one temporary file, and a crash mid-write must never leave
    half a credential where the login is read from.
    """
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".login-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(login), handle, indent=2)
        os.replace(temporary, login_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def clear_login() -> bool:
    """Delete the saved login; ``False`` if there was none."""
    try:
        login_path().unlink()
    except FileNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


def normalize_url(raw: str) -> str:
    """The server's origin as a login stores it: no trailing slash, no API prefix.

    Plain ``http://`` is refused for anything but this machine: the login is a
    bearer token, and it would otherwise cross the network in the clear.
    """
    url = raw.strip().rstrip("/")
    if url.endswith(API_PREFIX):
        url = url[: -len(API_PREFIX)].rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteError(f"{raw!r} is not a server URL: expected https://<host>")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        raise RemoteError(
            f"{raw!r} is plain http: a sign-in token must not cross the network "
            "unencrypted. Use the https:// URL (http is accepted for localhost only)."
        )
    return url


def evaluation_link(url: str, evaluation_id: str) -> str:
    """The web UI's page for one evaluation (the SPA reads the ``#/`` route)."""
    return f"{url}/#/evals/{evaluation_id}"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

#: Tests swap in an ``httpx`` transport here (an in-process ASGI app, a mocked
#: Cognito); ``None`` is the real network.
transport: httpx.AsyncBaseTransport | None = None


def http_client() -> httpx.AsyncClient:
    """The client every remote call goes through.

    No read timeout: an event stream is quiet for as long as a run takes. The
    connect timeout still bounds a server that is simply not there.
    """
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(30.0, read=None),
        follow_redirects=False,
    )


def _envelope_message(response: httpx.Response) -> str:
    """The server's own error sentence, falling back to the status line."""
    try:
        error = response.json().get("error") or {}
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    except (ValueError, AttributeError):
        pass
    return f"HTTP {response.status_code} {response.reason_phrase}".strip()


async def discover(http: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """``GET /health`` -- which pool to sign in to, and which lanes are on."""
    try:
        response = await http.get(f"{url}{API_PREFIX}/health")
    except httpx.HTTPError as exc:
        raise RemoteError(f"could not reach {url}: {exc}") from None
    if response.status_code != 200:
        raise RemoteError(f"{url} is not a Nimbus server ({_envelope_message(response)})")
    try:
        body = response.json()
    except ValueError:
        raise RemoteError(f"{url} is not a Nimbus server (its /health is not JSON)") from None
    if not isinstance(body, dict) or "auth" not in body:
        raise RemoteError(f"{url} is not a Nimbus server (its /health has no auth block)")
    return body


# --------------------------------------------------------------------------- #
# Cognito
# --------------------------------------------------------------------------- #


@dataclass
class Tokens:
    id_token: str
    expires_at: float
    refresh_token: str | None = None


@dataclass
class NewPasswordRequired:
    """An invited user's first sign-in: the temporary password must be replaced."""

    session: str


def checked_pool(auth: dict[str, Any], url: str) -> dict[str, str]:
    """The ``region`` and ``client_id`` of a ``/health`` auth block, or a refusal.

    Only an AWS region name is accepted, and it must be the one the user pool
    id names: the password is about to be sent to an endpoint built from it,
    so it is checked against what a real pool looks like, not trusted.
    """
    region = auth.get("region")
    client_id = auth.get("client_id")
    pool = _USER_POOL_ID.fullmatch(str(auth.get("user_pool_id") or ""))
    if (
        not isinstance(region, str)
        or not _REGION.fullmatch(region)
        or not isinstance(client_id, str)
        or not _CLIENT_ID.fullmatch(client_id)
        or pool is None
        or pool.group("region") != region
    ):
        raise RemoteError(
            f"{url} published a sign-in configuration that is not a Cognito user pool; "
            "refusing to send a password to it"
        )
    return {"region": region, "client_id": client_id}


class Cognito:
    """The three user pool calls the CLI needs, over plain HTTPS.

    The same calls the web UI makes (no SDK): ``InitiateAuth`` for a password
    or a refresh token, ``RespondToAuthChallenge`` for a first sign-in, and
    ``RevokeToken`` on logout.
    """

    def __init__(self, http: httpx.AsyncClient, region: str, client_id: str) -> None:
        # Checked here as well as at login: a saved login file is input too.
        if not _REGION.fullmatch(region) or not _CLIENT_ID.fullmatch(client_id):
            raise RemoteError("the saved sign-in names no valid user pool: run `nimbus login`")
        self._http = http
        self._endpoint = f"https://cognito-idp.{region}.amazonaws.com/"
        self._client_id = client_id

    async def _call(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                self._endpoint,
                content=json.dumps(body),
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": f"AWSCognitoIdentityProviderService.{operation}",
                },
            )
        except httpx.HTTPError as exc:
            raise RemoteError(f"could not reach the sign-in service: {exc}") from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200:
            kind = str(payload.get("__type", "")).split("#")[-1]
            message = _COGNITO_MESSAGES.get(kind) or payload.get("message") or kind
            raise RemoteError(f"sign-in failed: {message or response.status_code}")
        return payload

    @staticmethod
    def _tokens(result: dict[str, Any], refresh_token: str | None = None) -> Tokens:
        return Tokens(
            id_token=result["IdToken"],
            expires_at=time.time() + int(result.get("ExpiresIn", 3600)),
            refresh_token=result.get("RefreshToken") or refresh_token,
        )

    async def sign_in(self, email: str, password: str) -> Tokens | NewPasswordRequired:
        payload = await self._call(
            "InitiateAuth",
            {
                "AuthFlow": "USER_PASSWORD_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            },
        )
        if payload.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
            return NewPasswordRequired(session=payload["Session"])
        if "AuthenticationResult" not in payload:
            challenge = payload.get("ChallengeName", "an unknown challenge")
            raise RemoteError(f"sign-in needs {challenge}, which the CLI cannot answer")
        return self._tokens(payload["AuthenticationResult"])

    async def set_new_password(self, email: str, new_password: str, session: str) -> Tokens:
        payload = await self._call(
            "RespondToAuthChallenge",
            {
                "ChallengeName": "NEW_PASSWORD_REQUIRED",
                "ClientId": self._client_id,
                "Session": session,
                "ChallengeResponses": {"USERNAME": email, "NEW_PASSWORD": new_password},
            },
        )
        return self._tokens(payload["AuthenticationResult"])

    async def refresh(self, refresh_token: str) -> Tokens:
        payload = await self._call(
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            },
        )
        # A refresh returns a new ID token but not a new refresh token.
        return self._tokens(payload["AuthenticationResult"], refresh_token)

    async def revoke(self, refresh_token: str) -> None:
        await self._call("RevokeToken", {"Token": refresh_token, "ClientId": self._client_id})


def cognito_for(http: httpx.AsyncClient, login: Login) -> Cognito:
    assert login.auth is not None
    return Cognito(http, login.auth["region"], login.auth["client_id"])


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


class RemoteApi:
    """Authenticated calls to one harness, refreshing the token as needed.

    A refreshed token is saved back immediately, so the next command starts
    from it rather than from a stale one.
    """

    def __init__(self, http: httpx.AsyncClient, login: Login) -> None:
        self._http = http
        self.login = login

    async def _refresh(self) -> None:
        if not self.login.refresh_token:
            raise NotSignedInError(
                f"your sign-in to {self.login.url} has expired: run `nimbus login`"
            )
        try:
            tokens = await cognito_for(self._http, self.login).refresh(self.login.refresh_token)
        except RemoteError:
            raise NotSignedInError(
                f"your sign-in to {self.login.url} has expired: run `nimbus login`"
            ) from None
        self.login.id_token = tokens.id_token
        self.login.expires_at = tokens.expires_at
        save_login(self.login)

    async def _headers(self) -> dict[str, str]:
        if self.login.auth is None:
            return {}
        if not self.login.id_token:
            raise NotSignedInError(f"not signed in to {self.login.url}: run `nimbus login`")
        if (self.login.expires_at or 0) - time.time() < REFRESH_MARGIN_SECONDS:
            await self._refresh()
        return {"Authorization": f"Bearer {self.login.id_token}"}

    def _build(self, method: str, path: str, headers: dict[str, str], body: Any) -> httpx.Request:
        return self._http.build_request(
            method, f"{self.login.url}{API_PREFIX}{path}", headers=headers, json=body
        )

    async def _send(self, method: str, path: str, *, body: Any = None, stream: bool = False):
        """One request, retried once through a refresh if the server says 401."""
        try:
            response = await self._http.send(
                self._build(method, path, await self._headers(), body), stream=stream
            )
            if response.status_code == 401 and self.login.auth is not None:
                await response.aclose()
                await self._refresh()
                response = await self._http.send(
                    self._build(method, path, await self._headers(), body), stream=stream
                )
        except httpx.HTTPError as exc:
            raise UnreachableError(f"could not reach {self.login.url}: {exc}") from None
        if response.status_code >= 400:
            if stream:
                await response.aread()
            message = _envelope_message(response)
            await response.aclose()
            if response.status_code == 401:
                raise NotSignedInError(f"{self.login.url} refused the sign-in: {message}")
            if response.status_code == 404:
                raise RemoteNotFoundError(message)
            raise RemoteError(message)
        return response

    async def _json(self, method: str, path: str, *, body: Any = None) -> Any:
        response = await self._send(method, path, body=body)
        return response.json() if response.content else None

    async def health(self) -> dict[str, Any]:
        return await discover(self._http, self.login.url)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """``GET`` an API path; ``None``-valued parameters are left out."""
        given = {key: value for key, value in (params or {}).items() if value is not None}
        query = urlencode(given)
        return await self._json("GET", f"{path}?{query}" if query else path)

    async def run(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """``POST /runs`` as a stream: each NDJSON event, as it arrives.

        Closing the generator (Ctrl-C) closes the connection, which is what
        tells the server to cancel the run and store it as ``cancelled``.
        """
        response = await self._send("POST", "/runs", body=body, stream=True)
        try:
            async for line in response.aiter_lines():
                if line.strip():
                    yield json.loads(line)
        except httpx.TransportError as exc:
            raise UnreachableError(f"lost the run's stream from {self.login.url}: {exc}") from None
        finally:
            await response.aclose()

    async def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/evaluations", body=body)

    async def evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/evaluations/{evaluation_id}")

    async def cancel(self, evaluation_id: str) -> None:
        with contextlib.suppress(RemoteError):  # a 409 means it had already finished
            await self._send("DELETE", f"/evaluations/{evaluation_id}")

    async def events(self, evaluation_id: str) -> AsyncIterator[dict[str, Any]]:
        """Every event of the evaluation, in order, ending with ``eval_complete``.

        The server replays the whole log to each new subscriber, so a stream
        that drops (a proxy's request timeout, a flaky network) is re-opened
        and the lines already seen are skipped by position. ``eval_complete``
        is always passed through, because a server that lost the job's log
        replays that one line alone.
        """
        seen = 0
        failures = 0
        while True:
            position = 0
            try:
                response = await self._send(
                    "GET", f"/evaluations/{evaluation_id}/events", stream=True
                )
                try:
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        position += 1
                        entry = json.loads(line)
                        final = entry.get("type") == "eval_complete"
                        if position <= seen and not final:
                            continue
                        seen = max(seen, position)
                        failures = 0
                        yield entry
                        if final:
                            return
                finally:
                    await response.aclose()
            except (httpx.TransportError, UnreachableError) as exc:
                # Only a connection that failed or broke is retried: a server
                # that *answered* no (404, 401) has said all it is going to.
                last_error: Exception | None = exc
            else:
                last_error = None
            failures += 1
            if failures > MAX_RECONNECTS:
                reason = f": {last_error}" if last_error else ""
                raise RemoteError(
                    f"lost the event stream for {evaluation_id}{reason}; it may still be "
                    f"running -- see {evaluation_link(self.login.url, evaluation_id)}"
                )
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS * min(failures, 5))
