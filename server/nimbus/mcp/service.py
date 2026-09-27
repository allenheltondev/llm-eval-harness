"""The operations behind ``/mcp-servers``, shared by the router and the CLI.

The CLI runs these directly against the local store when it is not signed in
to a stack, so both front doors apply the same URL policy and header-patch
rules.
"""

from __future__ import annotations

from typing import Any

from nimbus import deployment
from nimbus.errors import BadRequestError
from nimbus.mcp import client as mcp_client
from nimbus.mcp.schemas import McpServer, McpServerCreate, McpServerUpdate, McpTestResult
from nimbus.mcp.store import McpServerStore
from nimbus.mcp.urls import validate_url


def create(store: McpServerStore, body: McpServerCreate) -> McpServer:
    url = validate_url(body.url, deployed=deployment.in_lambda())
    return store.create(name=body.name, url=url, headers=body.headers)


def update(store: McpServerStore, server_id: str, body: McpServerUpdate) -> McpServer:
    current = store.get(server_id)
    changes: dict[str, Any] = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.url is not None:
        changes["url"] = validate_url(body.url, deployed=deployment.in_lambda())
    try:
        changes["headers"] = body.apply_headers(current.headers)
    except ValueError as exc:
        raise BadRequestError(str(exc), detail={"field": "headers"}) from exc
    return store.save(current.model_copy(update=changes))


def test(store: McpServerStore, server_id: str) -> McpTestResult:
    """Connect with the saved URL and headers and list the server's tools.

    A server that cannot be reached is a *result* (``ok: false``), not an
    error: the request itself -- "try this server" -- succeeded.
    """
    server = store.get(server_id)
    try:
        tools = mcp_client.list_server_tools(server, deployed=deployment.in_lambda())
    except (mcp_client.McpConnectionError, BadRequestError) as exc:
        return McpTestResult(ok=False, error=exc.message)
    return McpTestResult(ok=True, tools=tools)
