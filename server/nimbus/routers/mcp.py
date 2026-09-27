"""Saved MCP servers: CRUD plus a connection test.

Responses carry a server's header *names* only. Values are write-only: set on
create, patched on update (``null`` removes one), used to connect, never read
back.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from nimbus import deployment
from nimbus.errors import BadRequestError
from nimbus.mcp import client as mcp_client
from nimbus.mcp.schemas import McpServerCreate, McpServerUpdate, McpTestResult
from nimbus.mcp.store import McpServerStore, get_mcp_store
from nimbus.mcp.urls import validate_url

router = APIRouter(tags=["mcp"])


@router.get("/mcp-servers")
def list_mcp_servers(store: McpServerStore = Depends(get_mcp_store)) -> dict[str, Any]:
    """Every saved MCP server, oldest first."""
    return {"servers": [s.public().model_dump(mode="json") for s in store.list()]}


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
def create_mcp_server(
    body: McpServerCreate, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    url = validate_url(body.url, deployed=deployment.in_lambda())
    server = store.create(name=body.name, url=url, headers=body.headers)
    return server.public().model_dump(mode="json")


@router.get("/mcp-servers/{server_id}")
def get_mcp_server(
    server_id: str, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    return store.get(server_id).public().model_dump(mode="json")


@router.put("/mcp-servers/{server_id}")
def update_mcp_server(
    server_id: str, body: McpServerUpdate, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
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
    return store.save(current.model_copy(update=changes)).public().model_dump(mode="json")


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(server_id: str, store: McpServerStore = Depends(get_mcp_store)) -> None:
    store.delete(server_id)


@router.post("/mcp-servers/{server_id}/test")
def test_mcp_server(
    server_id: str, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    """Connect with the saved URL and headers and list the server's tools.

    A server that cannot be reached is a *result* (``ok: false``), not an
    HTTP error: the request itself -- "try this server" -- succeeded.
    """
    server = store.get(server_id)
    try:
        tools = mcp_client.list_server_tools(server, deployed=deployment.in_lambda())
    except (mcp_client.McpConnectionError, BadRequestError) as exc:
        return McpTestResult(ok=False, error=exc.message).model_dump(mode="json")
    return McpTestResult(ok=True, tools=tools).model_dump(mode="json")
