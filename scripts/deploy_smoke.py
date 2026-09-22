#!/usr/bin/env python3
"""Post-deploy smoke test: does the deployed stack actually serve a request?

**This is not** ``scripts/live_smoke.py``. That one is gated behind
``RUN_LIVE_BEDROCK=1``, needs AWS credentials, calls Bedrock and costs money,
so it can never run in CI. This one needs **no credentials, makes no model
calls and spends nothing** -- every check below is an anonymous HTTP request
against the public front door. That is what makes it safe to run on every
deploy, and why it is a separate script rather than a flag on the other one.

It exists because of a specific, real failure: ``evalharness serve`` shipped
completely broken -- it died with ``asyncio.run() cannot be called from a
running event loop`` -- and passed every check in the pipeline, twice. Unit
tests, coverage and mutation testing all measure code *as imported*. Nothing
in CI ever executed the thing and watched it answer. This does.

What it proves
--------------
* The Lambda Web Adapter boots and FastAPI is serving (``/health`` answers with
  the real payload, not a CloudFront error page).
* Auth is switched on in this deployment, and the pool the SPA needs to sign in
  against is published.
* A protected route refuses an anonymous caller with the API's own 401
  envelope -- so the request reached the application, rather than being
  bounced by CloudFront or dying in a 5xx.
* The SPA is served at ``/`` and deep links resolve, so CloudFront's routing to
  ``index.html`` works rather than returning S3's 403/404.

What it deliberately does **not** prove
---------------------------------------
Anything needing a real token: that ``Authorization`` survives CloudFront, that
NDJSON streaming works end to end, that Cognito sign-in succeeds. Those need a
test user and credentials. In particular the header question is *unanswerable*
this way on purpose: ``auth.UnauthorizedError`` returns one message for both a
missing and an invalid token, so a bogus bearer cannot be told apart from a
stripped one. Better security, narrower smoke test -- an honest trade, and not
one to paper over by pretending this covers it.

Usage::

    python3 scripts/deploy_smoke.py --url https://d111111abcdef8.cloudfront.net

Exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

API_PREFIX = "/api/v1"

#: A freshly-created CloudFront distribution can 404 for a little while before
#: the origin and behaviours propagate, so the first check is allowed to retry.
#: Later checks are not: by then the front door has demonstrably answered.
HEALTH_ATTEMPTS = 10
HEALTH_RETRY_SECONDS = 15
REQUEST_TIMEOUT_SECONDS = 30

#: Every field the SPA needs from `/health` before it can render a sign-in form.
REQUIRED_AUTH_KEYS = ("provider", "region", "user_pool_id", "client_id")


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass
class Results:
    """What passed, what failed, in the order the checks ran."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(name)
        print(f"PASS  {name}" + (f"  ({detail})" if detail else ""), flush=True)

    def fail(self, name: str, why: str) -> None:
        self.failed.append(name)
        print(f"FAIL  {name}\n      {why}", flush=True)


class Unreachable(Exception):
    """No HTTP response at all -- DNS, TLS, connection refused, timeout.

    Distinct from an error *status*, which is a perfectly good answer that some
    of the checks below are asserting on. Raised rather than returned so that
    no check can accidentally treat "the host is down" as "the host said 404".
    """


def fetch(url: str, *, headers: dict[str, str] | None = None) -> Response:
    """GET ``url``. An HTTP error status is a response here, not an exception."""
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as raw:
            return Response(raw.status, raw.read(), _lower_keys(raw.headers))
    except urllib.error.HTTPError as exc:
        # 401/404 are outcomes this script asserts on, so they are not failures
        # of the request itself.
        return Response(exc.code, exc.read(), _lower_keys(exc.headers))
    except Exception as exc:
        raise Unreachable(f"{type(exc).__name__}: {exc}") from None


def _lower_keys(headers: Any) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def check_health(
    base: str,
    results: Results,
    attempts: int = HEALTH_ATTEMPTS,
    retry_seconds: float = HEALTH_RETRY_SECONDS,
) -> dict[str, Any] | None:
    """The adapter booted and FastAPI is answering with its real payload."""
    name = "health responds"
    url = f"{base}{API_PREFIX}/health"
    last = ""

    for attempt in range(1, attempts + 1):
        try:
            response = fetch(url)
        except Unreachable as exc:
            last = str(exc)
        else:
            if response.status == 200:
                try:
                    payload = response.json()
                except ValueError:
                    last = f"200 but the body is not JSON: {response.body[:200]!r}"
                else:
                    if payload.get("status") == "ok":
                        results.ok(name, f"attempt {attempt}")
                        return payload
                    last = f"200 but status was {payload.get('status')!r}"
            else:
                last = f"HTTP {response.status}: {response.body[:200]!r}"

        if attempt < attempts:
            print(f"  ... {last} -- retrying in {retry_seconds}s", flush=True)
            time.sleep(retry_seconds)

    results.fail(name, f"gave up after {attempts} attempts. Last: {last}")
    return None


def check_auth_published(payload: dict[str, Any], results: Results) -> None:
    """Auth is on here, and the SPA has what it needs to sign a user in."""
    name = "auth is required and published"
    block = payload.get("auth")
    if not isinstance(block, dict):
        results.fail(name, f"/health carried no auth object: {payload.get('auth')!r}")
        return
    if block.get("required") is not True:
        results.fail(
            name,
            "auth.required is not true -- this deployment is serving every route "
            "to anonymous callers.",
        )
        return
    missing = [key for key in REQUIRED_AUTH_KEYS if not block.get(key)]
    if missing:
        results.fail(name, f"auth block is missing {', '.join(missing)}")
        return
    results.ok(name, f"pool {block['user_pool_id']}")


def check_protected_route_rejects(base: str, results: Results) -> None:
    """A protected route answers 401 in the API's own envelope.

    The envelope is the point. A 401 from CloudFront, or a 5xx, would mean the
    request never reached the application.
    """
    name = "protected route refuses anonymous callers"
    try:
        response = fetch(f"{base}{API_PREFIX}/runs")
    except Unreachable as exc:
        results.fail(name, str(exc))
        return
    if response.status != 401:
        results.fail(name, f"expected 401, got {response.status}: {response.body[:200]!r}")
        return
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        results.fail(name, f"401 but not the API error envelope: {response.body[:200]!r}")
        return
    if code != "unauthorized":
        results.fail(name, f"401 with an unexpected code: {code!r}")
        return
    results.ok(name)


def check_spa(base: str, results: Results) -> None:
    """The SPA is served at the root and deep links resolve to it."""
    for name, path in (("SPA is served at /", "/"), ("SPA deep link resolves", "/runs")):
        try:
            response = fetch(base + path)
        except Unreachable as exc:
            results.fail(name, str(exc))
            continue
        if response.status != 200:
            results.fail(
                name,
                f"expected 200, got {response.status}. A 403/404 here means CloudFront "
                f"is not routing to index.html: {response.body[:200]!r}",
            )
            continue
        if "html" not in response.content_type:
            results.fail(name, f"200 but content-type was {response.content_type!r}")
            continue
        results.ok(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deploy_smoke",
        description="Anonymous post-deploy checks. No credentials, no spend.",
    )
    parser.add_argument("--url", required=True, help="Base URL of the deployed app")
    parser.add_argument(
        "--attempts",
        type=int,
        default=HEALTH_ATTEMPTS,
        help=f"health retries while CloudFront propagates (default: {HEALTH_ATTEMPTS})",
    )
    parser.add_argument(
        "--retry-seconds",
        type=float,
        default=HEALTH_RETRY_SECONDS,
        help=f"seconds between health retries (default: {HEALTH_RETRY_SECONDS})",
    )
    args = parser.parse_args(argv)

    base = args.url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        print(f"deploy_smoke: --url must be absolute, got {args.url!r}", file=sys.stderr)
        return 2

    print(f"Smoking {base}\n", flush=True)
    results = Results()

    payload = check_health(base, results, args.attempts, args.retry_seconds)
    if payload is not None:
        check_auth_published(payload, results)
        check_protected_route_rejects(base, results)
    check_spa(base, results)

    print(f"\n{len(results.passed)} passed, {len(results.failed)} failed", flush=True)
    if results.failed:
        print("Failed: " + ", ".join(results.failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
