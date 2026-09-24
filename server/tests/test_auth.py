"""Bearer-token auth (nimbus.auth): the gate every non-health route sits behind.

Tokens are minted here with a throwaway RSA key and the JWKS endpoint is
served by respx, so every case is hermetic: no Cognito, no network.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, Request
from jwt.algorithms import RSAAlgorithm

from nimbus import auth
from nimbus.config import Settings

REGION = "us-east-1"
POOL_ID = "us-east-1_TestPool1"
CLIENT_ID = "1example23456789abcdefghijkl"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

#: A route from every protected router, and the open one, by name.
PROTECTED_ROUTES = ("/api/v1/models", "/api/v1/runs", "/api/v1/guardrails", "/api/v1/tools")


class _Signer:
    """A signing key, its JWKS document, and a token factory."""

    def __init__(self, kid: str = "key-1") -> None:
        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = self._private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_jwk = RSAAlgorithm.to_jwk(self._private.public_key(), as_dict=True)
        self.jwks = {"keys": [{**public_jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}

    def token(self, *, kid: str | None = None, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "token_use": "id",
            "aud": CLIENT_ID,
            "sub": "user-123",
            "email": "person@example.com",
            "iat": now,
            "exp": now + 3600,
        }
        for key, value in overrides.items():
            if value is None:
                claims.pop(key, None)
            else:
                claims[key] = value
        return jwt.encode(claims, self._pem, algorithm="RS256", headers={"kid": kid or self.kid})


@pytest.fixture
def signer() -> _Signer:
    return _Signer()


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setenv("NIMBUS_AWS_REGION", REGION)
    monkeypatch.setenv("NIMBUS_AUTH_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("NIMBUS_AUTH_CLIENT_ID", CLIENT_ID)


@pytest.fixture
def jwks(signer: _Signer):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=signer.jwks))
        yield route


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_auth_off_unless_both_settings_present():
    assert auth.auth_enabled(Settings()) is False
    assert auth.auth_enabled(Settings(auth_user_pool_id=POOL_ID)) is False
    assert auth.auth_enabled(Settings(auth_client_id=CLIENT_ID)) is False
    assert auth.auth_enabled(Settings(auth_user_pool_id=POOL_ID, auth_client_id=CLIENT_ID))


def test_blank_auth_settings_mean_unset(monkeypatch):
    monkeypatch.setenv("NIMBUS_AUTH_USER_POOL_ID", "   ")
    monkeypatch.setenv("NIMBUS_AUTH_CLIENT_ID", CLIENT_ID)
    assert auth.auth_enabled(Settings()) is False


def test_auth_config_derives_issuer_and_jwks_url():
    config = auth.auth_config(
        Settings(aws_region=REGION, auth_user_pool_id=POOL_ID, auth_client_id=CLIENT_ID)
    )
    assert config is not None
    assert config.issuer == ISSUER
    assert config.jwks_url == JWKS_URL


def test_health_block_off():
    assert auth.health_block(Settings()) == {"required": False, "supports_required_group": True}


def test_health_block_on_publishes_sign_in_inputs():
    settings = Settings(aws_region="eu-west-1", auth_user_pool_id=POOL_ID, auth_client_id=CLIENT_ID)
    assert auth.health_block(settings) == {
        "required": True,
        "provider": "cognito",
        "region": "eu-west-1",
        "user_pool_id": POOL_ID,
        "client_id": CLIENT_ID,
        "required_group": None,
        "supports_required_group": True,
    }


def test_health_block_names_the_required_group():
    settings = Settings(
        auth_user_pool_id=POOL_ID, auth_client_id=CLIENT_ID, auth_required_group="nimbus"
    )
    assert auth.health_block(settings)["required_group"] == "nimbus"


def test_a_blank_required_group_means_none(monkeypatch):
    monkeypatch.setenv("NIMBUS_AUTH_REQUIRED_GROUP", "  ")
    assert Settings().auth_required_group is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("bearer abc", "abc"),
        ("Bearer   ", None),
        ("Basic abc", None),
        ("", None),
    ],
)
def test_bearer_token_parsing(header: str, expected: str | None):
    scope = {
        "type": "http",
        "headers": [(b"authorization", header.encode())] if header else [],
    }
    assert auth.bearer_token(Request(scope)) == expected


# --------------------------------------------------------------------------- #
# the gate, through the app
# --------------------------------------------------------------------------- #


async def test_auth_off_leaves_every_route_open(client):
    response = await client.get("/api/v1/health")
    assert response.json()["auth"] == {"required": False, "supports_required_group": True}
    # No bearer token and yet not a 401: the guardrails listing needs AWS
    # credentials it does not have, but the *gate* let it through.
    response = await client.get("/api/v1/runs")
    assert response.status_code == 200


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_health_stays_open_and_advertises_the_pool(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["auth"] == {
        "required": True,
        "provider": "cognito",
        "region": REGION,
        "user_pool_id": POOL_ID,
        "client_id": CLIENT_ID,
        "required_group": None,
        "supports_required_group": True,
    }


@pytest.mark.usefixtures("auth_enabled", "jwks")
@pytest.mark.parametrize("path", PROTECTED_ROUTES)
async def test_missing_token_is_401_on_every_protected_router(client, path: str):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "A valid bearer token is required",
            "detail": None,
        }
    }


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_post_without_token_is_rejected_before_validation(client):
    """A 401, not a 422: the gate runs before the body is even parsed."""
    response = await client.post("/api/v1/runs", json={"not": "a run"})
    assert response.status_code == 401


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_valid_id_token_passes(client, signer: _Signer):
    response = await client.get("/api/v1/runs", headers=_bearer(signer.token()))
    assert response.status_code == 200


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_valid_access_token_passes(client, signer: _Signer):
    token = signer.token(token_use="access", aud=None, client_id=CLIENT_ID)
    response = await client.get("/api/v1/runs", headers=_bearer(token))
    assert response.status_code == 200


@pytest.mark.usefixtures("auth_enabled", "jwks")
@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("expired", {"exp": int(time.time()) - 120}),
        ("wrong issuer", {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"}),
        ("id token for another client", {"aud": "someone-elses-client"}),
        (
            "access token for another client",
            {"token_use": "access", "aud": None, "client_id": "someone-elses-client"},
        ),
        ("unknown token_use", {"token_use": "refresh"}),
        ("missing token_use", {"token_use": None}),
        ("missing exp", {"exp": None}),
    ],
)
async def test_bad_claims_are_401(client, signer: _Signer, label: str, overrides: dict[str, Any]):
    response = await client.get("/api/v1/runs", headers=_bearer(signer.token(**overrides)))
    assert response.status_code == 401, label
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_token_signed_by_another_key_is_401(client, signer: _Signer):
    imposter = _Signer(kid=signer.kid)  # same kid, different key
    response = await client.get("/api/v1/runs", headers=_bearer(imposter.token()))
    assert response.status_code == 401


@pytest.mark.usefixtures("auth_enabled", "jwks")
@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c", ""])
async def test_garbage_bearer_is_401(client, token: str):
    response = await client.get("/api/v1/runs", headers=_bearer(token))
    assert response.status_code == 401


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_token_without_kid_is_401(client, signer: _Signer):
    token = jwt.encode({"iss": ISSUER}, signer._pem, algorithm="RS256")
    assert "kid" not in jwt.get_unverified_header(token)
    response = await client.get("/api/v1/runs", headers=_bearer(token))
    assert response.status_code == 401


@pytest.mark.usefixtures("auth_enabled")
async def test_unsigned_algorithm_none_is_401(client):
    with respx.mock:
        respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": []}))
        token = jwt.encode(
            {"iss": ISSUER, "token_use": "id", "aud": CLIENT_ID},
            key=None,
            algorithm="none",
            headers={"kid": "key-1"},
        )
        response = await client.get("/api/v1/runs", headers=_bearer(token))
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# the JWKS cache
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("auth_enabled")
async def test_jwks_fetched_once_and_reused(client, signer: _Signer, jwks):
    for _ in range(3):
        response = await client.get("/api/v1/runs", headers=_bearer(signer.token()))
        assert response.status_code == 200
    assert jwks.call_count == 1


@pytest.mark.usefixtures("auth_enabled")
async def test_unknown_kid_refetches_once_then_rejects(client, signer: _Signer, jwks):
    assert (await client.get("/api/v1/runs", headers=_bearer(signer.token()))).status_code == 200
    response = await client.get("/api/v1/runs", headers=_bearer(signer.token(kid="rotated")))
    assert response.status_code == 401
    assert jwks.call_count == 2


@pytest.mark.usefixtures("auth_enabled")
async def test_key_rotation_is_picked_up_on_refetch(client, signer: _Signer):
    rotated = _Signer(kid="key-2")
    with respx.mock as mock:
        route = mock.get(JWKS_URL).mock(
            side_effect=[
                httpx.Response(200, json=signer.jwks),
                httpx.Response(200, json={"keys": signer.jwks["keys"] + rotated.jwks["keys"]}),
            ]
        )
        assert (
            await client.get("/api/v1/runs", headers=_bearer(signer.token()))
        ).status_code == 200
        assert (
            await client.get("/api/v1/runs", headers=_bearer(rotated.token()))
        ).status_code == 200
        # both keys are now cached: no third fetch
        assert (
            await client.get("/api/v1/runs", headers=_bearer(signer.token()))
        ).status_code == 200
    assert route.call_count == 2


@pytest.mark.usefixtures("auth_enabled")
@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, text="not json"), httpx.ConnectError("down")],
)
async def test_jwks_unavailable_is_502_not_401(client, signer: _Signer, response):
    with respx.mock as mock:
        if isinstance(response, Exception):
            mock.get(JWKS_URL).mock(side_effect=response)
        else:
            mock.get(JWKS_URL).mock(return_value=response)
        result = await client.get("/api/v1/runs", headers=_bearer(signer.token()))
    assert result.status_code == 502
    assert result.json()["error"] == {
        "code": "upstream_error",
        "message": "Could not fetch the user pool's signing keys",
        "detail": {"jwks_url": JWKS_URL},
    }


@pytest.mark.usefixtures("auth_enabled")
async def test_unusable_jwks_entries_are_skipped(client, signer: _Signer):
    document = {
        "keys": [
            {"kid": "no-kty"},
            {"kty": "RSA", "kid": 42},
            {"kty": "oct", "kid": "wrong-type", "k": "AAAA", "alg": "HS256"},
            *signer.jwks["keys"],
        ]
    }
    with respx.mock as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=document))
        response = await client.get("/api/v1/runs", headers=_bearer(signer.token()))
    assert response.status_code == 200


@pytest.mark.usefixtures("auth_enabled")
async def test_jwk_without_alg_defaults_to_rs256(client, signer: _Signer):
    stripped = [{k: v for k, v in key.items() if k != "alg"} for key in signer.jwks["keys"]]
    with respx.mock as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": stripped}))
        response = await client.get("/api/v1/runs", headers=_bearer(signer.token()))
    assert response.status_code == 200


@pytest.mark.usefixtures("auth_enabled", "jwks")
async def test_verified_claims_land_on_request_state(app, signer: _Signer):
    seen: dict[str, Any] = {}

    @app.get("/api/v1/_test/whoami", dependencies=[Depends(auth.require_auth)])
    async def _whoami(request: Request):
        seen.update(request.state.user)
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/api/v1/_test/whoami", headers=_bearer(signer.token()))
    assert response.status_code == 200
    assert seen["sub"] == "user-123"
    assert seen["email"] == "person@example.com"


# --------------------------------------------------------------------------- #
# the required group: a shared pool authenticates, the group authorizes
# --------------------------------------------------------------------------- #


@pytest.fixture
def group_required(auth_enabled, monkeypatch):
    monkeypatch.setenv("NIMBUS_AUTH_REQUIRED_GROUP", "nimbus")


@pytest.mark.usefixtures("group_required", "jwks")
async def test_a_member_of_the_required_group_passes(client, signer: _Signer):
    token = signer.token(**{"cognito:groups": ["rsc-free", "nimbus"]})

    response = await client.get("/api/v1/runs", headers=_bearer(token))

    assert response.status_code == 200


@pytest.mark.usefixtures("group_required", "jwks")
@pytest.mark.parametrize(
    "groups",
    [None, [], ["rsc-free"], ["rsc-pro", "nimbus-admins"], "nimbus"],
    ids=["no-groups-claim", "empty", "other-group", "prefix-only", "string-not-list"],
)
async def test_a_valid_token_outside_the_group_is_403(client, signer: _Signer, groups):
    token = signer.token(**{"cognito:groups": groups})

    response = await client.get("/api/v1/runs", headers=_bearer(token))

    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "forbidden"
    assert body["detail"] == {"required_group": "nimbus"}


@pytest.mark.usefixtures("group_required", "jwks")
async def test_an_access_token_carries_its_groups_too(client, signer: _Signer):
    member = signer.token(
        token_use="access", aud=None, client_id=CLIENT_ID, **{"cognito:groups": ["nimbus"]}
    )
    outsider = signer.token(token_use="access", aud=None, client_id=CLIENT_ID)

    assert (await client.get("/api/v1/runs", headers=_bearer(member))).status_code == 200
    assert (await client.get("/api/v1/runs", headers=_bearer(outsider))).status_code == 403


@pytest.mark.usefixtures("group_required", "jwks")
async def test_an_invalid_token_is_still_401_not_403(client, signer: _Signer):
    token = signer.token(aud="another-client", **{"cognito:groups": ["nimbus"]})

    response = await client.get("/api/v1/runs", headers=_bearer(token))

    assert response.status_code == 401


@pytest.mark.usefixtures("group_required")
async def test_health_stays_open_and_names_the_group(client):
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["auth"]["required_group"] == "nimbus"
