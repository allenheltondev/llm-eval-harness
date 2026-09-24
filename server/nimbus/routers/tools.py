"""``GET /tools`` -- the toolsets a run may name (``RunRequest.toolset``)."""

from typing import Any

from fastapi import APIRouter

from nimbus.tools.registry import list_handlers

router = APIRouter(tags=["tools"])


@router.get("/tools")
def list_tools() -> dict[str, Any]:
    """Every registered toolset with the tool names it exposes to the model."""
    return {"toolsets": [{"name": name, "tools": tools} for name, tools in list_handlers().items()]}
