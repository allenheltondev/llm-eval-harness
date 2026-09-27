"""Saved MCP servers: schemas, URL policy, both stores, the API, and runs.

The connection tests talk to a real MCP server over streamable HTTP
(``tests/mcp_http_server.py``), so the transport, the saved auth header and
the tool-name prefix are exercised end to end rather than mocked.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime

import httpcore
import httpx
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
from nimbus.mcp import transport as mcp_transport
from nimbus.mcp.schemas import McpServer, McpServerCreate, McpServerUpdate
from nimbus.mcp.transport import PublicOnlyBackend, build_http_client
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


class ParameterNotFound(Exception):
    pass


class FakeSsm:
    """The slice of the boto3 SSM client SsmHeaderSecrets uses."""

    class exceptions:  # noqa: N801 - boto3 spelling
        ParameterNotFound = ParameterNotFound

    def __init__(self):
        self.parameters: dict[str, dict] = {}
        self.gets = 0

    def put_parameter(self, **kwargs):
        assert kwargs["Type"] == "SecureString" and kwargs["Overwrite"] is True
        self.parameters[kwargs["Name"]] = kwargs

    def get_parameter(self, Name, WithDecryption):  # noqa: N803 - boto3 spelling
        assert WithDecryption is True
        self.gets += 1
        if Name not in self.parameters:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Name": Name, "Value": self.parameters[Name]["Value"]}}

    def delete_parameter(self, Name):  # noqa: N803 - boto3 spelling
        if Name not in self.parameters:
            raise ParameterNotFound(Name)
        del self.parameters[Name]


def _dynamo(table=None):
    ssm = FakeSsm()
    secrets = mcp_store.SsmHeaderSecrets("/nimbus/stack/mcp/", "us-east-1", client=ssm)
    return mcp_store.DynamoMcpStore(table if table is not None else FakeTable(), secrets), ssm


def test_dynamo_store_keeps_header_values_out_of_the_table():
    table = FakeTable()
    store, ssm = _dynamo(table)
    created = store.create(name="One", url="https://x/mcp", headers={"Authorization": "tok"})
    store.create(name="Two", url="https://y/mcp", headers={})

    item = table.items[0]
    assert item["pk"] == f"MCP#{created.id}" and item["sk"] == "META"
    assert item["GSI1PK"] == "MCP"
    assert item["header_names"] == ["Authorization"]
    assert "tok" not in json.dumps(table.items)
    assert list(ssm.parameters) == [f"/nimbus/stack/mcp/{created.id}"]
    assert json.loads(ssm.parameters[f"/nimbus/stack/mcp/{created.id}"]["Value"]) == {
        "Authorization": "tok"
    }

    listed = store.list()
    assert [s.name for s in listed] == ["One", "Two"]
    assert listed[0].public().header_names == ["Authorization"]
    assert ssm.gets == 0  # listing never reads a secret

    assert store.get(created.id).headers == {"Authorization": "tok"}
    saved = store.save(store.get(created.id).model_copy(update={"headers": {}}))
    assert saved.headers == {}
    assert ssm.parameters == {}  # no headers left: the parameter goes too
    assert store.get(created.id).headers == {}

    store.save(saved.model_copy(update={"headers": {"X-Key": "k"}}))
    store.delete(created.id)
    assert ssm.parameters == {}
    assert [s.name for s in store.list()] == ["Two"]
    with pytest.raises(NotFoundError):
        store.get(created.id)
    with pytest.raises(NotFoundError):
        store.delete(created.id)
    with pytest.raises(NotFoundError):
        store.save(saved)


def test_dynamo_store_reports_headers_that_went_missing():
    store, ssm = _dynamo()
    created = store.create(name="One", url="https://x/mcp", headers={"A": "b"})
    ssm.parameters.clear()
    with pytest.raises(mcp_store.McpStoreUnavailableError, match="missing"):
        store.get(created.id)


class FlakyTable(FakeTable):
    """A table whose next put/delete can be told to fail, like a throttled write."""

    def __init__(self):
        super().__init__()
        self.fail_next: str | None = None

    def put_item(self, Item):  # noqa: N803 - boto3 spelling
        if self.fail_next == "put":
            self.fail_next = None
            raise RuntimeError("ProvisionedThroughputExceeded")
        return super().put_item(Item=Item)

    def delete_item(self, Key):  # noqa: N803 - boto3 spelling
        if self.fail_next == "delete":
            self.fail_next = None
            raise RuntimeError("ProvisionedThroughputExceeded")
        return super().delete_item(Key=Key)


def test_a_failed_create_leaves_no_parameter_behind():
    table = FlakyTable()
    store, ssm = _dynamo(table)
    table.fail_next = "put"
    with pytest.raises(RuntimeError):
        store.create(name="One", url="https://x/mcp", headers={"Authorization": "tok"})
    assert ssm.parameters == {}  # the id was never returned: nothing may remain
    assert store.list() == []


@pytest.mark.parametrize("new_headers", [{"Authorization": "new"}, {}])
def test_a_failed_update_restores_the_previous_headers(new_headers):
    table = FlakyTable()
    store, ssm = _dynamo(table)
    created = store.create(name="One", url="https://x/mcp", headers={"Authorization": "old"})
    table.fail_next = "put"
    with pytest.raises(RuntimeError):
        store.save(created.model_copy(update={"headers": new_headers}))
    # The item still names the old header, and its value is the old one.
    assert store.get(created.id).headers == {"Authorization": "old"}


def test_a_delete_that_fails_part_way_can_be_retried():
    table = FlakyTable()
    store, ssm = _dynamo(table)
    created = store.create(name="One", url="https://x/mcp", headers={"Authorization": "tok"})

    # SSM fails: nothing is gone yet, so the server is still there to retry.
    def unavailable(**_kwargs):
        raise RuntimeError("ssm unavailable")

    original = ssm.delete_parameter
    ssm.delete_parameter = unavailable
    with pytest.raises(RuntimeError, match="ssm unavailable"):
        store.delete(created.id)
    assert store.get(created.id).headers == {"Authorization": "tok"}
    ssm.delete_parameter = original

    # The table fails after the secret is gone: the retry still finds the
    # server and finishes, the missing parameter being no obstacle.
    table.fail_next = "delete"
    with pytest.raises(RuntimeError):
        store.delete(created.id)
    assert ssm.parameters == {}
    store.delete(created.id)
    assert store.list() == []


def test_dynamo_store_follows_list_pagination():
    class PagedTable(FakeTable):
        def query(self, **kwargs):
            kwargs["Limit"] = 1
            return super().query(**kwargs)

    store, _ = _dynamo(PagedTable())
    for name in ("a", "b", "c"):
        store.create(name=name, url="https://x/mcp", headers={})
    assert [s.name for s in store.list()] == ["a", "b", "c"]


def test_ssm_secrets_build_their_client_lazily(monkeypatch):
    import boto3

    ssm = FakeSsm()
    monkeypatch.setattr(boto3, "client", lambda service, region_name: ssm)
    secrets = mcp_store.SsmHeaderSecrets("/p", "eu-west-1")
    secrets.put("s", {"A": "b"})
    assert secrets.get("s") == {"A": "b"}


def test_headers_must_fit_one_standard_parameter():
    big = {f"X-{i}": "v" * 500 for i in range(9)}
    with pytest.raises(ValidationError, match="bytes in total"):
        McpServerCreate(name="s", url="https://x/mcp", headers=big)
    with pytest.raises(ValueError, match="bytes in total"):
        McpServerUpdate(headers={"X-9": "v" * 2000}).apply_headers(
            {f"X-{i}": "v" * 500 for i in range(4)}
        )


def test_store_selection(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert isinstance(mcp_store.build_mcp_store(Settings()), mcp_store.SqliteMcpStore)

    dynamo = mcp_store.build_mcp_store(Settings(mcp_table="t"))
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


# --------------------------------------------------------------------------- #
# Connection rules: enforced by the transport, not a preflight
# --------------------------------------------------------------------------- #


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Stands in for the real network: records where it was asked to connect.

    ``connect_to`` sends the socket to the in-process test server whatever
    address was asked for, so a request can complete end to end while the test
    still sees the address the guard chose.
    """

    def __init__(self, connect_to: tuple[str, int] | None = None, fail: set[str] = frozenset()):
        self.connected: list[str] = []
        self._connect_to = connect_to
        self._fail = fail
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        if host in self._fail or self._connect_to is None:
            raise httpcore.ConnectError(f"cannot reach {host}")
        return await self._real.connect_tcp(*self._connect_to, timeout=timeout)

    async def connect_unix_socket(
        self, path, timeout=None, socket_options=None
    ):  # pragma: no cover
        raise NotImplementedError

    async def sleep(self, seconds):
        await self._real.sleep(seconds)


def _resolving(*answers):
    """A resolver that gives each successive lookup the next answer."""
    queue = list(answers)
    calls = []

    def resolve(host, port):
        calls.append(host)
        return queue.pop(0)

    resolve.calls = calls
    return resolve


async def test_rebinding_name_cannot_reach_a_private_address():
    # The same name answers public, then private (DNS rebinding). Each socket
    # goes to the address that was checked for it -- never to a fresh lookup.
    resolve = _resolving(["93.184.216.34"], ["10.0.0.7"])
    inner = RecordingBackend(fail={"93.184.216.34"})
    backend = PublicOnlyBackend(inner=inner, resolve=resolve)

    with pytest.raises(httpcore.ConnectError, match="cannot reach 93.184.216.34"):
        await backend.connect_tcp("rebind.example.com", 443)
    with pytest.raises(httpcore.ConnectError, match=r"non-public address \(10\.0\.0\.7\)"):
        await backend.connect_tcp("rebind.example.com", 443)

    # The inner network only ever saw the validated literal, so it had no name
    # of its own to re-resolve; the private answer never reached a socket.
    assert inner.connected == ["93.184.216.34"]
    assert resolve.calls == ["rebind.example.com", "rebind.example.com"]


@pytest.mark.parametrize(
    "answer",
    [
        ["93.184.216.34", "127.0.0.1"],  # one bad answer blocks the lot
        ["169.254.169.254"],
        ["::ffff:10.0.0.1"],
        ["fe80::1%eth0"],
        [],
    ],
)
async def test_any_non_public_answer_refuses_the_connection(answer):
    inner = RecordingBackend()
    backend = PublicOnlyBackend(inner=inner, resolve=lambda host, port: answer)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("mixed.example.com", 443)
    assert inner.connected == []


async def test_unresolvable_names_and_unix_sockets_are_refused():
    def fail(host, port):
        raise socket.gaierror("no such host")

    backend = PublicOnlyBackend(inner=RecordingBackend(), resolve=fail)
    with pytest.raises(httpcore.ConnectError, match="Cannot resolve"):
        await backend.connect_tcp("nope.example.com", 443)
    with pytest.raises(httpcore.ConnectError, match="Unix sockets"):
        await backend.connect_unix_socket("/var/run/docker.sock")
    await backend.sleep(0)


async def test_the_next_public_address_is_tried_when_one_fails(mcp_url):
    port = int(mcp_url.split(":")[2].split("/")[0])
    inner = RecordingBackend(connect_to=("127.0.0.1", port), fail={"93.184.216.34"})
    backend = PublicOnlyBackend(
        inner=inner, resolve=lambda host, p: ["93.184.216.34", "93.184.216.35"]
    )
    stream = await backend.connect_tcp("multi.example.com", port)
    await stream.aclose()
    assert inner.connected == ["93.184.216.34", "93.184.216.35"]


async def test_deployed_client_routes_every_connection_through_the_guard(mcp_url):
    # Proves the httpx client really uses the backend (not a pool built
    # around it): the request succeeds only because the guard picked the
    # validated public address, which the recording network then serves.
    port = int(mcp_url.split(":")[2].split("/")[0])
    inner = RecordingBackend(connect_to=("127.0.0.1", port))
    guard = PublicOnlyBackend(inner=inner, resolve=lambda host, p: ["93.184.216.34"])
    async with build_http_client({"X-Api-Key": API_KEY}, deployed=True, backend=guard) as http:
        response = await http.get(f"http://mcp.example.com:{port}/nothing-here")
    assert response.status_code == 404
    assert inner.connected == ["93.184.216.34"]

    blocked = PublicOnlyBackend(inner=inner, resolve=lambda host, p: ["127.0.0.1"])
    async with build_http_client({}, deployed=True, backend=blocked) as http:
        with pytest.raises(httpx.ConnectError, match="non-public"):
            await http.get(f"http://mcp.example.com:{port}/mcp")


def test_deployed_client_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    client = build_http_client({}, deployed=True)
    assert client._trust_env is False
    assert not client._mounts


@pytest.mark.parametrize("deployed", [False, True])
def test_redirects_are_never_followed(mcp_url, deployed, monkeypatch):
    # A saved endpoint answering 307 elsewhere: the client must stop there,
    # with the saved headers never sent to the redirect target.
    from tests import mcp_http_server

    if deployed:
        # The test server is on loopback; let the guard's resolver say it is
        # public so only the redirect rule is under test here.
        port = int(mcp_url.split(":")[2].split("/")[0])
        monkeypatch.setattr(
            mcp_transport,
            "PublicOnlyBackend",
            lambda: PublicOnlyBackend(
                inner=RecordingBackend(connect_to=("127.0.0.1", port)),
                resolve=lambda host, p: ["93.184.216.34"],
            ),
        )
        monkeypatch.setattr(mcp_client, "check_reachable", lambda url, deployed: None)
    server = _server(url=mcp_url.replace("/mcp", "/redirect"), headers={"X-Api-Key": API_KEY})

    with pytest.raises(mcp_client.McpConnectionError, match="redirects are not followed"):
        mcp_client.list_server_tools(server, deployed=deployed)
    assert mcp_http_server.REDIRECT_TARGET_HITS == []


async def test_default_deployed_client_refuses_loopback(mcp_url):
    # No test doubles: the real resolver and network, as a deployed Lambda
    # would use them. The test server is on loopback, so it must be unreachable.
    async with build_http_client({}, deployed=True) as http:
        with pytest.raises(httpx.ConnectError, match=r"non-public address \(127\.0\.0\.1\)"):
            await http.get(mcp_url)
    async with build_http_client({}, deployed=False) as http:
        assert (await http.get(mcp_url.replace("/mcp", "/nothing"))).status_code == 401
