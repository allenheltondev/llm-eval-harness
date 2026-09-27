"""Saved MCP servers: CRUD plus a connection test.

Responses carry a server's header *names* only. Values are write-only: set on
create, patched on update (``null`` removes one), used to connect, never read
back.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from nimbus.mcp import service
from nimbus.mcp.schemas import McpServerCreate, McpServerUpdate
from nimbus.mcp.store import McpServerStore, get_mcp_store

router = APIRouter(tags=["mcp"])


@router.get("/mcp-servers")
def list_mcp_servers(store: McpServerStore = Depends(get_mcp_store)) -> dict[str, Any]:
    """Every saved MCP server, oldest first."""
    return {"servers": [s.public().model_dump(mode="json") for s in store.list()]}


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
def create_mcp_server(
    body: McpServerCreate, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    return service.create(store, body).public().model_dump(mode="json")


@router.get("/mcp-servers/{server_id}")
def get_mcp_server(
    server_id: str, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    return store.get(server_id).public().model_dump(mode="json")


@router.put("/mcp-servers/{server_id}")
def update_mcp_server(
    server_id: str, body: McpServerUpdate, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    return service.update(store, server_id, body).public().model_dump(mode="json")


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(server_id: str, store: McpServerStore = Depends(get_mcp_store)) -> None:
    store.delete(server_id)


@router.post("/mcp-servers/{server_id}/test")
def test_mcp_server(
    server_id: str, store: McpServerStore = Depends(get_mcp_store)
) -> dict[str, Any]:
    """Connect with the saved URL and headers and list the server's tools."""
    return service.test(store, server_id).model_dump(mode="json")
