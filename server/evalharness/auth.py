"""Bearer-token authentication against a Cognito user pool.

The deployed server is the *only* gate between the internet and the AWS
account's Bedrock bill: its Lambda Function URL is ``AuthType: NONE`` and the
CloudFront distribution in front of it is a second unauthenticated door
(``docs/serverless-deploy-infra.md`` explains why CloudFront OAC cannot close
either one for a browser that POSTs). So the gate lives here, in the
application, where it works identically through both doors.

The pattern is the one ``readysetcloud/rsc-core`` uses: a Cognito user pool
plus an app client with ``USER_PASSWORD_AUTH``; the browser talks to
``cognito-idp`` directly (no Hosted UI, no SDK) and sends the resulting **ID
token** as ``Authorization: Bearer <jwt>``. Access tokens are accepted too, so
a non-browser client that prefers them is not locked out.

Configuration
-------------
Two settings switch the whole thing on; both come from the stack in a deployed
server and are simply absent locally::

    EVALHARNESS_AUTH_USER_POOL_ID   e.g. us-east-1_AbCdEfGhI
    EVALHARNESS_AUTH_CLIENT_ID      the app client id

With either unset :func:`auth_enabled` is false and :func:`require_auth` is a
no-op -- ``make dev``, the fake-model mode and the E2E suite run exactly as
before. With both set every router except ``/health`` requires a valid token
(``/health`` is where the SPA learns *that* auth is required, and which pool
to sign in against, so it has to stay reachable signed out).

Verification
------------
Signature and claims are checked with PyJWT against the pool's JWKS
(``https://cognito-idp.<region>.amazonaws.com/<pool>/.well-known/jwks.json``),
which is fetched once per process and re-fetched when a token names a key id
the cache has not seen (Cognito rotates keys). Beyond the signature and
``exp``, the checks are the ones Cognito documents for verifying its tokens:
``iss`` must be the pool, ``token_use`` must be ``id`` or ``access``, and the
client must match -- ``aud`` on an ID token, ``client_id`` on an access token.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Depends, Request, status
from jwt import PyJWK, PyJWTError

from evalharness.config import Settings, get_settings
from evalharness.errors import AppError, UpstreamError

logger = logging.getLogger(__name__)

#: The only signature algorithm Cognito user pools use.
_ALGORITHMS = ["RS256"]

#: The ``token_use`` values a Cognito pool mints, and the claim each one
#: carries the app client id in.
_CLIENT_CLAIM_BY_TOKEN_USE = {"id": "aud", "access": "client_id"}


class UnauthorizedError(AppError):
    """The request carried no usable bearer token.

    One status, one code: which check failed is logged, not disclosed -- a
    caller probing the API learns nothing from the message beyond "sign in".
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


@dataclass(frozen=True)
class AuthConfig:
    """Everything the SPA needs to sign in, and the server needs to verify."""

    region: str
    user_pool_id: str
    client_id: str

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


def auth_config(settings: Settings) -> AuthConfig | None:
    """The pool this server verifies against, or ``None`` when auth is off."""
    if not settings.auth_user_pool_id or not settings.auth_client_id:
        return None
    return AuthConfig(
        region=settings.aws_region,
        user_pool_id=settings.auth_user_pool_id,
        client_id=settings.auth_client_id,
    )


def auth_enabled(settings: Settings) -> bool:
    """Whether every non-health route requires a bearer token."""
    return auth_config(settings) is not None


def health_block(settings: Settings) -> dict[str, Any]:
    """The ``auth`` object ``GET /health`` publishes.

    ``required`` tells the SPA whether to show a sign-in screen at all; the
    other three are exactly the inputs its Cognito calls need. None of them is
    a secret -- a user pool id and a public app client id are visible to every
    browser that signs in anyway.
    """
    config = auth_config(settings)
    if config is None:
        return {"required": False}
    return {
        "required": True,
        "provider": "cognito",
        "region": config.region,
        "user_pool_id": config.user_pool_id,
        "client_id": config.client_id,
    }


class JwksCache:
    """Process-lifetime cache of a pool's signing keys, keyed by ``kid``.

    A miss on a *known* pool re-fetches once (key rotation); a miss after a
    fresh fetch is a token this pool did not sign. Concurrent first requests
    share one fetch through the lock rather than each hitting Cognito.
    """

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, PyJWK]] = {}
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        self._keys.clear()

    async def key_for(self, config: AuthConfig, kid: str) -> PyJWK | None:
        keys = self._keys.get(config.jwks_url)
        if keys is not None and kid in keys:
            return keys[kid]
        async with self._lock:
            keys = self._keys.get(config.jwks_url)
            if keys is None or kid not in keys:
                keys = await self._fetch(config.jwks_url)
                self._keys[config.jwks_url] = keys
        return keys.get(kid)

    async def _fetch(self, url: str) -> dict[str, PyJWK]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Nothing can be verified without the keys, and that is the
            # server's problem, not the caller's: a 502, not a 401.
            raise UpstreamError(
                "Could not fetch the user pool's signing keys",
                detail={"jwks_url": url},
            ) from exc
        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if not isinstance(kid, str):
                continue
            try:
                keys[kid] = PyJWK(entry, algorithm=entry.get("alg") or _ALGORITHMS[0])
            except PyJWTError:
                logger.warning("Skipping unusable JWK %s from %s", kid, url)
        return keys


#: The one cache the dependency below reads. Tests reset it between cases.
jwks_cache = JwksCache()


def bearer_token(request: Request) -> str | None:
    """The token from ``Authorization: Bearer <token>``, or ``None``."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


async def verify_token(token: str, config: AuthConfig) -> dict[str, Any]:
    """Return the verified claims of ``token`` or raise :class:`UnauthorizedError`."""
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as exc:
        raise UnauthorizedError("A valid bearer token is required") from exc
    kid = header.get("kid")
    if not isinstance(kid, str):
        raise UnauthorizedError("A valid bearer token is required")

    key = await jwks_cache.key_for(config, kid)
    if key is None:
        logger.info("Rejected token signed by unknown key id %s", kid)
        raise UnauthorizedError("A valid bearer token is required")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=_ALGORITHMS,
            issuer=config.issuer,
            # `aud` is only present on ID tokens; the client check below
            # covers both token kinds explicitly instead.
            options={"verify_aud": False, "require": ["exp", "iss", "token_use"]},
        )
    except PyJWTError as exc:
        logger.info("Rejected bearer token: %s", exc)
        raise UnauthorizedError("A valid bearer token is required") from exc

    token_use = claims.get("token_use")
    client_claim = _CLIENT_CLAIM_BY_TOKEN_USE.get(token_use)
    if client_claim is None or claims.get(client_claim) != config.client_id:
        logger.info("Rejected %s token issued for another client", token_use)
        raise UnauthorizedError("A valid bearer token is required")
    return claims


async def require_auth(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Router-level dependency: reject the request unless it carries a valid token.

    A no-op when auth is not configured. On success the verified claims are
    left on ``request.state.user`` for any handler that wants the caller's
    identity (nothing does yet -- the harness is single-tenant).
    """
    config = auth_config(settings)
    if config is None:
        return
    token = bearer_token(request)
    if token is None:
        raise UnauthorizedError("A valid bearer token is required")
    request.state.user = await verify_token(token, config)
