"""Saved MCP servers: schemas, URL policy, both stores, the API, and runs.

The connection tests talk to a real MCP server over streamable HTTP
(``tests/mcp_http_server.py``), so the transport, the saved auth header and
the tool-name prefix are exercised end to end rather than mocked.
"""

from __future__ import annotations

import base64
import json
import socket
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from nimbus.config import Settings
from nimbus.engine import runner
from nimbus.engine.events import ErrorEvent, RunCompleteEvent, RunStartEvent, ToolResultEvent
from nimbus.engine.fake_model import FakeModel, Text, ToolUseStep
from nimbus.engine.schemas import RunRequest
from nimbus.errors import BadRequestError, NotFoundError
from nimbus.mcp import client as mcp_client
from nimbus.mcp import store as mcp_store
from nimbus.mcp.schemas import McpServer, McpServerCreate, McpServerUpdate
from nimbus.mcp.urls import McpUrlError, check_reachable, validate_url
from nimbus.store import db, history
from tests.fake_table import FakeTable
from tests.mcp_http_server import API_KEY, running_mcp_server


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "mcp.db"))


@pytest.fixture(scope="module")
def mcp_url():
    with running_mcp_server() as url:
        yield url


def _server(**overrides) -> McpServer:
    now = datetime.now(UTC)
    fields = {
        "id": "srv1",
        "name": "Test Server",
        "url": "https://mcp.example.com/mcp",
        "headers": {},
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return McpServer(**fields)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


def test_create_accepts_headers_and_strips_the_name():
    body = McpServerCreate(name="  GitHub ", url="https://x/mcp", headers={"Authorization": "t"})
    assert body.name == "GitHub"
    assert body.headers == {"Authorization": "t"}


@pytest.mark.parametrize(
    "headers",
    [
        {"Bad Name": "v"},
        {"": "v"},
        {"Host": "evil"},
        {"Mcp-Session-Id": "x"},
        {"X-Key": "line\r\nInjected: yes"},
        {"X-Key": "v" * 2049},
        {"X-Key": "a", "x-key": "b"},
        {f"X-{i}": "v" for i in range(11)},
    ],
)
def test_create_rejects_bad_headers(headers):
    with pytest.raises(ValidationError):
        McpServerCreate(name="s", url="https://x/mcp", headers=headers)


def test_blank_names_are_rejected():
    with pytest.raises(ValidationError):
        McpServerCreate(name="   ", url="https://x/mcp")
    with pytest.raises(ValidationError):
        McpServerUpdate(name="  ")
    assert McpServerUpdate(name=" n ").name == "n"


def test_header_patch_sets_replaces_removes_and_keeps():
    current = {"Authorization": "old", "X-Team": "core"}
    patch = McpServerUpdate(headers={"authorization": "new", "X-Team": None, "X-Extra": "1"})
    assert patch.apply_headers(current) == {"authorization": "new", "X-Extra": "1"}
    assert McpServerUpdate().apply_headers(current) == current
    with pytest.raises(ValidationError):
        McpServerUpdate(headers={"Host": "x"})


def test_header_patch_enforces_the_limit():
    current = {f"X-{i}": "v" for i in range(10)}
    with pytest.raises(ValueError, match="at most"):
        McpServerUpdate(headers={"X-New": "v"}).apply_headers(current)


def test_public_view_has_header_names_only():
    public = _server(headers={"b": "secret", "a": "secret"}).public()
    assert public.header_names == ["a", "b"]
    assert "secret" not in public.model_dump_json()


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #


def test_local_allows_http_and_localhost():
    assert (
        validate_url(" http://localhost:8000/mcp ", deployed=False) == "http://localhost:8000/mcp"
    )
    check_reachable("http://127.0.0.1:1/mcp", deployed=False)


@pytest.mark.parametrize(
    "url",
    ["ftp://x/mcp", "not a url", "https:///mcp", "https://user:pw@x/mcp", "http://x:99999/"],
)
def test_malformed_urls_are_rejected_everywhere(url):
    with pytest.raises(McpUrlError):
        validate_url(url, deployed=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://localhost/mcp",
        "https://api.localhost/mcp",
        "https://db.internal/mcp",
        "https://127.0.0.1/mcp",
        "https://10.0.0.5/mcp",
        "https://169.254.169.254/latest",
        "https://[::1]/mcp",
        "https://[::ffff:10.0.0.1]/mcp",
    ],
)
def test_deployed_requires_public_https(url):
    with pytest.raises(McpUrlError):
        validate_url(url, deployed=True)


def test_deployed_accepts_public_https():
    assert validate_url("https://mcp.example.com/mcp", deployed=True)
    assert validate_url("https://8.8.8.8/mcp", deployed=True)


def _resolving_to(monkeypatch, *addresses):
    def fake_getaddrinfo(host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_deployed_connect_rejects_a_name_resolving_privately(monkeypatch):
    _resolving_to(monkeypatch, "93.184.216.34", "10.1.2.3")
    with pytest.raises(McpUrlError, match="publicly reachable"):
        check_reachable("https://rebind.example.com/mcp", deployed=True)


def test_deployed_connect_accepts_a_public_name(monkeypatch):
    _resolving_to(monkeypatch, "93.184.216.34")
    check_reachable("https://mcp.example.com/mcp", deployed=True)


def test_deployed_connect_reports_unresolvable_hosts(monkeypatch):
    def fail(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(McpUrlError, match="Cannot resolve"):
        check_reachable("https://nope.example.com/mcp", deployed=True)


# --------------------------------------------------------------------------- #
# Tool prefixes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "taken", "expected"),
    [
        ("GitHub MCP", [], "github_mcp"),
        ("  !!! ", [], "mcp"),
        ("42 things", [], "mcp_42_things"),
        ("A very long server name indeed", [], "a_very_long_serv"),
        ("GitHub", ["github"], "github_2"),
        ("GitHub", ["github", "github_2"], "github_3"),
        ("A very long server name indeed", ["a_very_long_serv"], "a_very_long_se_2"),
    ],
)
def test_tool_prefix(name, taken, expected):
    assert mcp_client.tool_prefix(name, taken) == expected


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #


def test_sqlite_store_round_trip(initialized_db):
    store = mcp_store.SqliteMcpStore()
    first = store.create(name="One", url="http://localhost/mcp", headers={"X-Key": "k"})
    second = store.create(name="Two", url="http://localhost:2/mcp", headers={})

    assert [s.id for s in store.list()] == [first.id, second.id]
    loaded = store.get(first.id)
    assert loaded.headers == {"X-Key": "k"}
    assert loaded.created_at.tzinfo is not None

    saved = store.save(loaded.model_copy(update={"name": "Uno", "headers": {}}))
    assert saved.name == "Uno"
    assert store.get(first.id).headers == {}
    assert saved.updated_at >= loaded.updated_at

    store.delete(second.id)
    assert [s.id for s in store.list()] == [first.id]
    for call in (
        lambda: store.get("missing"),
        lambda: store.delete("missing"),
        lambda: store.save(_server(id="missing")),
    ):
        with pytest.raises(NotFoundError):
            call()


class FakeCipher:
    def __init__(self):
        self.decrypts = 0

    def encrypt(self, headers, server_id):
        return base64.b64encode(json.dumps([server_id, headers]).encode()).decode()

    def decrypt(self, blob, server_id):
        self.decrypts += 1
        bound_to, headers = json.loads(base64.b64decode(blob))
        assert bound_to == server_id
        return headers


def test_dynamo_store_round_trip_keeps_headers_encrypted():
    table, cipher = FakeTable(), FakeCipher()
    store = mcp_store.DynamoMcpStore(table, cipher)
    created = store.create(name="One", url="https://x/mcp", headers={"Authorization": "tok"})
    store.create(name="Two", url="https://y/mcp", headers={})

    item = table.items[0]
    assert item["pk"] == f"MCP#{created.id}" and item["sk"] == "META"
    assert item["GSI1PK"] == "MCP"
    assert item["header_names"] == ["Authorization"]
    assert "tok" not in json.dumps(item)
    assert "headers_enc" not in table.items[1]

    listed = store.list()
    assert [s.name for s in listed] == ["One", "Two"]
    assert listed[0].public().header_names == ["Authorization"]
    assert cipher.decrypts == 0  # listing never decrypts

    assert store.get(created.id).headers == {"Authorization": "tok"}
    saved = store.save(store.get(created.id).model_copy(update={"headers": {}}))
    assert saved.headers == {}
    assert (
        "headers_enc" not in table.get_item(Key={"pk": f"MCP#{created.id}", "sk": "META"})["Item"]
    )

    store.delete(created.id)
    assert [s.name for s in store.list()] == ["Two"]
    with pytest.raises(NotFoundError):
        store.get(created.id)
    with pytest.raises(NotFoundError):
        store.delete(created.id)


def test_dynamo_store_follows_list_pagination():
    class PagedTable(FakeTable):
        def query(self, **kwargs):
            kwargs["Limit"] = 1
            return super().query(**kwargs)

    table = PagedTable()
    store = mcp_store.DynamoMcpStore(table, FakeCipher())
    for name in ("a", "b", "c"):
        store.create(name=name, url="https://x/mcp", headers={})
    assert [s.name for s in store.list()] == ["a", "b", "c"]


class FakeKms:
    def __init__(self):
        self.calls = []

    def encrypt(self, **kwargs):
        self.calls.append(("encrypt", kwargs))
        return {"CiphertextBlob": b"C:" + kwargs["Plaintext"]}

    def decrypt(self, **kwargs):
        self.calls.append(("decrypt", kwargs))
        return {"Plaintext": kwargs["CiphertextBlob"][2:]}


def test_kms_cipher_binds_ciphertext_to_the_server():
    kms = FakeKms()
    cipher = mcp_store.KmsHeaderCipher("alias/nimbus", "us-east-1", client=kms)
    blob = cipher.encrypt({"A": "b"}, "srv1")
    assert cipher.decrypt(blob, "srv1") == {"A": "b"}
    (_, enc), (_, dec) = kms.calls
    assert enc["KeyId"] == "alias/nimbus"
    assert enc["EncryptionContext"] == dec["EncryptionContext"] == {"nimbus:mcp-server": "srv1"}


def test_kms_cipher_without_a_key_refuses_to_store_headers():
    cipher = mcp_store.KmsHeaderCipher(None, "us-east-1", client=FakeKms())
    with pytest.raises(mcp_store.McpStoreUnavailableError):
        cipher.encrypt({"A": "b"}, "srv1")


def test_kms_cipher_builds_its_client_lazily(monkeypatch):
    import boto3

    kms = FakeKms()
    monkeypatch.setattr(boto3, "client", lambda service, region_name: kms)
    cipher = mcp_store.KmsHeaderCipher("k", "eu-west-1")
    assert cipher.decrypt(cipher.encrypt({"A": "b"}, "s"), "s") == {"A": "b"}


def test_store_selection(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert isinstance(mcp_store.build_mcp_store(Settings()), mcp_store.SqliteMcpStore)

    dynamo = mcp_store.build_mcp_store(Settings(mcp_table="t", mcp_kms_key_id="k"))
    assert isinstance(dynamo, mcp_store.DynamoMcpStore)

    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    with pytest.raises(mcp_store.McpStoreUnavailableError):
        mcp_store.build_mcp_store(Settings())


# --------------------------------------------------------------------------- #
# Connecting
# --------------------------------------------------------------------------- #


def test_list_server_tools_sends_the_saved_header(mcp_url):
    server = _server(url=mcp_url, headers={"X-Api-Key": API_KEY})
    tools = mcp_client.list_server_tools(server, deployed=False)
    assert {t.name for t in tools} == {"echo", "add"}
    assert next(t for t in tools if t.name == "echo").description == "Echo the text back, reversed."


def test_list_server_tools_reports_a_refused_connection(mcp_url):
    with pytest.raises(mcp_client.McpConnectionError, match="401"):
        mcp_client.list_server_tools(_server(url=mcp_url), deployed=False)


def test_resolve_dedupes_and_rejects_unknown_ids(initialized_db):
    store = mcp_store.SqliteMcpStore()
    saved = store.create(name="One", url="http://localhost/mcp", headers={})
    assert [s.id for s in mcp_client.resolve([saved.id, saved.id], store)] == [saved.id]
    with pytest.raises(BadRequestError) as info:
        mcp_client.resolve(["nope"], store)
    assert info.value.code == "unknown_mcp_server"


def test_describe_digs_out_the_root_cause():
    try:
        try:
            raise ExceptionGroup("group", [ValueError("root cause\nmore detail")])
        except ExceptionGroup as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as exc:
        assert mcp_client.describe(exc) == "root cause"
    assert mcp_client.describe(RuntimeError()) == "RuntimeError"


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


async def test_crud_never_returns_header_values(client, initialized_db):
    created = await client.post(
        "/api/v1/mcp-servers",
        json={
            "name": "GitHub",
            "url": "http://localhost:9/mcp",
            "headers": {"Authorization": "tok"},
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["header_names"] == ["Authorization"]
    assert "tok" not in created.text
    server_id = body["id"]

    listed = await client.get("/api/v1/mcp-servers")
    assert [s["id"] for s in listed.json()["servers"]] == [server_id]
    assert "tok" not in listed.text
    assert (await client.get(f"/api/v1/mcp-servers/{server_id}")).json()["name"] == "GitHub"

    updated = await client.put(
        f"/api/v1/mcp-servers/{server_id}",
        json={"name": "GH", "url": "http://localhost:10/mcp", "headers": {"X-Team": "core"}},
    )
    assert updated.status_code == 200
    assert updated.json()["header_names"] == ["Authorization", "X-Team"]
    assert mcp_store.SqliteMcpStore().get(server_id).headers == {
        "Authorization": "tok",
        "X-Team": "core",
    }

    removed = await client.put(
        f"/api/v1/mcp-servers/{server_id}", json={"headers": {"Authorization": None}}
    )
    assert removed.json()["header_names"] == ["X-Team"]
    assert removed.json()["url"] == "http://localhost:10/mcp"

    assert (await client.delete(f"/api/v1/mcp-servers/{server_id}")).status_code == 204
    assert (await client.get(f"/api/v1/mcp-servers/{server_id}")).status_code == 404


async def test_api_rejects_bad_input(client, initialized_db, monkeypatch):
    bad = await client.post("/api/v1/mcp-servers", json={"name": "x", "url": "ftp://x"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_mcp_url"

    created = await client.post(
        "/api/v1/mcp-servers", json={"name": "x", "url": "http://localhost/mcp"}
    )
    server_id = created.json()["id"]
    too_many = {f"X-{i}": "v" for i in range(10)}
    await client.put(f"/api/v1/mcp-servers/{server_id}", json={"headers": too_many})
    over = await client.put(f"/api/v1/mcp-servers/{server_id}", json={"headers": {"X-New": "v"}})
    assert over.status_code == 400

    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.setenv("NIMBUS_MCP_TABLE", "")
    deployed = await client.put(
        f"/api/v1/mcp-servers/{server_id}", json={"url": "http://localhost/mcp"}
    )
    assert deployed.status_code == 503  # no table: nowhere durable to keep it


async def test_api_test_endpoint(client, initialized_db, mcp_url):
    good = await client.post(
        "/api/v1/mcp-servers",
        json={"name": "T", "url": mcp_url, "headers": {"X-Api-Key": API_KEY}},
    )
    result = (await client.post(f"/api/v1/mcp-servers/{good.json()['id']}/test")).json()
    assert result["ok"] is True
    assert sorted(t["name"] for t in result["tools"]) == ["add", "echo"]

    bad = await client.post("/api/v1/mcp-servers", json={"name": "T", "url": mcp_url})
    result = (await client.post(f"/api/v1/mcp-servers/{bad.json()['id']}/test")).json()
    assert result["ok"] is False and "401" in result["error"]
    assert result["tools"] == []


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


async def _collect(request, model, **kwargs):
    return [e async for e in runner.execute_run(request, model_factory=lambda _r: model, **kwargs)]


def _stored(run_id):
    with Session(db.get_engine()) as session:
        return history.get_run(session, run_id)


async def test_run_calls_mcp_tools_alongside_the_builtin_toolset(initialized_db, mcp_url):
    store = mcp_store.SqliteMcpStore()
    server = store.create(name="Test Server", url=mcp_url, headers={"X-Api-Key": API_KEY})
    model = FakeModel(script=[ToolUseStep("test_server_echo", {"text": "abc"}), Text("done")])
    request = RunRequest(
        model_id="m",
        user_prompt="go",
        toolset="fraud-detection",
        mcp_servers=[server.id],
    )

    events = await _collect(request, model, mcp_store=store)

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [(r.name, r.output, r.error) for r in results] == [("test_server_echo", "cba", None)]
    assert isinstance(events[-1], RunCompleteEvent) and events[-1].status == "completed"
    record = _stored(events[0].run_id)
    assert record.config["mcp_servers"] == [server.id]
    assert record.config["toolset"] == "fraud-detection"
    assert API_KEY not in json.dumps(record.config)
    assert record.tool_transcript[0]["name"] == "test_server_echo"


async def test_run_reports_an_unreachable_server_in_band(initialized_db, mcp_url):
    store = mcp_store.SqliteMcpStore()
    server = store.create(name="Locked", url=mcp_url, headers={})
    request = RunRequest(model_id="m", user_prompt="go", mcp_servers=[server.id])

    events = await _collect(request, FakeModel(script=[Text("never")]), mcp_store=store)

    assert isinstance(events[0], RunStartEvent)
    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.code == "mcp_connection_failed"
    assert "'Locked'" in error.message and "401" in error.message
    assert _stored(events[0].run_id).status == "error"


async def test_run_rejects_an_unknown_server_before_it_starts(initialized_db):
    request = RunRequest(model_id="m", user_prompt="go", mcp_servers=["missing"])
    with pytest.raises(BadRequestError) as info:
        await _collect(request, FakeModel(script=[Text("x")]), mcp_store=mcp_store.SqliteMcpStore())
    assert info.value.code == "unknown_mcp_server"


async def test_run_without_mcp_servers_stores_no_key(initialized_db):
    events = await _collect(
        RunRequest(model_id="m", user_prompt="go"), FakeModel(script=[Text("x")])
    )
    assert "mcp_servers" not in _stored(events[0].run_id).config


def test_run_request_caps_mcp_servers():
    with pytest.raises(ValidationError):
        RunRequest(model_id="m", user_prompt="go", mcp_servers=[str(i) for i in range(6)])


async def test_runs_router_resolves_through_the_configured_store(client, initialized_db):
    response = await client.post(
        "/api/v1/runs",
        json={"model_id": "m", "user_prompt": "go", "mcp_servers": ["missing"], "stream": False},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_mcp_server"


async def test_evaluations_reject_unknown_servers_up_front(client, initialized_db):
    run_config = {"model_id": "m", "user_prompt": "go", "mcp_servers": ["missing"]}
    for body in (
        {"kind": "determinism", "run_config": run_config},
        {"kind": "determinism", "run_config": run_config, "execution": "cloud"},
        {
            "kind": "suite",
            "suite": {
                "run_config": {"model_id": "m", "mcp_servers": ["missing"]},
                "cases": [{"id": "c1", "input": "hi"}],
            },
        },
    ):
        response = await client.post("/api/v1/evaluations", json=body)
        assert response.status_code == 400, body
        assert response.json()["error"]["code"] == "unknown_mcp_server"


def test_explicit_nulls_in_an_update_mean_unchanged():
    patch = McpServerUpdate(name=None, url=None, headers=None)
    assert patch.apply_headers({"A": "b"}) == {"A": "b"}


async def test_a_non_mcp_failure_building_the_agent_is_not_blamed_on_mcp(
    initialized_db, mcp_url, monkeypatch
):
    store = mcp_store.SqliteMcpStore()
    server = store.create(name="Test Server", url=mcp_url, headers={"X-Api-Key": API_KEY})
    stopped = []
    monkeypatch.setattr(mcp_client, "stop_clients", lambda clients: stopped.extend(clients))

    def broken_agent(**_kwargs):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(runner, "Agent", broken_agent)
    request = RunRequest(model_id="m", user_prompt="go", mcp_servers=[server.id])

    events = await _collect(request, FakeModel(script=[Text("x")]), mcp_store=store)

    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.code == "internal_error" and error.message == "agent exploded"
    assert len(stopped) == 1  # no agent took the client, so the runner stopped it
