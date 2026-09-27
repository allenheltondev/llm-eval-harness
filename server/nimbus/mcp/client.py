"""Connecting a run to saved MCP servers.

Each server becomes one Strands :class:`~strands.tools.mcp.MCPClient` over the
streamable HTTP transport, passed straight into the agent's ``tools``. The
agent starts it (connects and lists its tools) when it is built and stops it
on ``agent.cleanup()``, so a client lives exactly as long as its run.

Tool names are prefixed with ``mcp-`` and a slug of the server's name
(``mcp-github_search``), so two servers that both offer ``search`` stay
distinguishable, and no MCP tool can take a built-in tool's name.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from typing import Any

from nimbus.errors import AppError, BadRequestError, NotFoundError
from nimbus.mcp.schemas import McpServer, McpToolInfo
from nimbus.mcp.store import McpServerStore
from nimbus.mcp.transport import build_http_client
from nimbus.mcp.urls import check_reachable

#: How long to wait for a server to accept the MCP handshake.
STARTUP_TIMEOUT_SECONDS = 20
#: Every MCP tool name starts with this; see :func:`tool_prefix`.
MCP_TOOL_NAMESPACE = "mcp-"
#: Room left for the tool's own name within a provider's 64-character limit.
MAX_SLUG_LENGTH = 16
MAX_SERVERS_PER_RUN = 5


class McpConnectionError(AppError):
    """A saved MCP server could not be reached when a run needed it."""

    status_code = 502
    code = "mcp_connection_failed"


def tool_prefix(name: str, taken: Iterable[str] = ()) -> str:
    """The tool-name prefix for a server: ``mcp-<slug>``, unique among ``taken``.

    MCP tools are named ``<prefix>_<tool>``. The prefix lives in a namespace no
    other tool can reach:

    * it contains ``-``, which a built-in ``@tool`` name (a Python identifier)
      never does, so no MCP tool can collide with a built-in one; and
    * the slug uses ``-`` between words, never ``_``, so the first ``_`` in a
      tool name always ends the prefix: two servers with distinct prefixes can
      never produce the same full name (``a`` + ``b_c`` vs ``a_b`` + ``c``).

    Hyphens are valid in tool names for every provider (``[a-zA-Z0-9_-]``).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:MAX_SLUG_LENGTH].strip("-")
    base = f"{MCP_TOOL_NAMESPACE}{slug or 'server'}"
    used = set(taken)
    candidate, n = base, 2
    while candidate in used:
        suffix = f"-{n}"
        candidate = base[: len(MCP_TOOL_NAMESPACE) + MAX_SLUG_LENGTH - len(suffix)] + suffix
        n += 1
    return candidate


def resolve(ids: Iterable[str], store: McpServerStore) -> list[McpServer]:
    """The definitions (secrets included) for a run's ``mcp_servers`` ids.

    Raises:
        BadRequestError: code ``unknown_mcp_server`` for an id that is not
            saved -- a 400 on the run that named it, not a 404 on the run.
    """
    servers: list[McpServer] = []
    for server_id in dict.fromkeys(ids):
        try:
            servers.append(store.get(server_id))
        except NotFoundError as exc:
            raise BadRequestError(
                f"MCP server {server_id!r} not found",
                detail={"mcp_server_id": server_id},
                code="unknown_mcp_server",
            ) from exc
    return servers


def _transport(server: McpServer, *, deployed: bool) -> Any:
    """The ``transport_callable`` for one server: a fresh connection per call."""
    from mcp.client.streamable_http import streamable_http_client

    @asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        # The transport only closes an HTTP client it created itself; this one
        # carries the saved headers and the connection rules, so it is opened
        # and closed here.
        async with (
            build_http_client(dict(server.headers), deployed=deployed) as http_client,
            streamable_http_client(server.url, http_client=http_client) as streams,
        ):
            yield streams

    return connect


def build_clients(servers: list[McpServer], *, deployed: bool) -> list[Any]:
    """One unstarted ``MCPClient`` per server, with distinct tool prefixes.

    Raises:
        McpUrlError: a saved URL that is no longer allowed (deployed: one
            whose host now resolves to a non-public address).
    """
    from strands.tools.mcp import MCPClient

    clients: list[Any] = []
    prefixes: list[str] = []
    for server in servers:
        check_reachable(server.url, deployed=deployed)
        prefix = tool_prefix(server.name, prefixes)
        prefixes.append(prefix)
        clients.append(
            MCPClient(
                _transport(server, deployed=deployed),
                startup_timeout=STARTUP_TIMEOUT_SECONDS,
                prefix=prefix,
                application_name="nimbus",
            )
        )
    return clients


def stop_clients(clients: list[Any]) -> None:
    """Best-effort stop for clients no agent took ownership of."""
    for client in clients:
        # Teardown must not mask the error that got us here.
        with suppress(Exception):
            client.stop(None, None, None)


def list_server_tools(server: McpServer, *, deployed: bool) -> list[McpToolInfo]:
    """Connect to ``server``, list its tools, and disconnect (the Test button).

    Blocking: call it from a threadpool.

    Raises:
        McpConnectionError: the server could not be reached or refused the
            handshake.
    """
    (client,) = build_clients([server], deployed=deployed)
    try:
        with client:
            tools = client.list_tools_sync()
    except Exception as exc:
        raise McpConnectionError(
            f"Could not connect to MCP server {server.name!r}: {describe(exc)}",
            detail={"mcp_server_id": server.id},
        ) from exc
    prefix = tool_prefix(server.name)
    return [
        McpToolInfo(
            name=tool.tool_name.removeprefix(f"{prefix}_"),
            description=tool.tool_spec.get("description"),
        )
        for tool in tools
    ]


def describe(exc: BaseException) -> str:
    """The most specific message in an exception chain (MCP wraps its causes)."""
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(messages) < 5:
        if isinstance(current, BaseExceptionGroup) and current.exceptions:
            current = current.exceptions[0]
            continue
        text = str(current).strip()
        if text and text not in messages:
            messages.append(text)
        current = current.__cause__ or current.__context__
    return messages[-1].splitlines()[0] if messages else exc.__class__.__name__
