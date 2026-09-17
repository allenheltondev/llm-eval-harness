"""Named toolsets a run may execute with (``RunRequest.toolset``).

A toolset is a list of Strands ``@tool`` callables under a stable name that
``GET /tools`` advertises and a run names to opt in. One example ships:
``fraud-detection``, a small in-memory account-risk workflow. Real tools
belong to whoever authors the evaluation; this is the seam they plug into.
"""

from __future__ import annotations

from evalharness.tools import fraud_detection

_REGISTRY: dict[str, list] = {
    "fraud-detection": [
        fraud_detection.freeze_account,
        fraud_detection.flag_suspicious_transaction,
        fraud_detection.create_fraud_alert,
        fraud_detection.update_risk_profile,
    ],
}


def get_tools(toolset: str) -> list:
    """The ``@tool`` callables registered under ``toolset`` (empty if unknown)."""
    return list(_REGISTRY.get(toolset, []))


def has_handler(toolset: str, tool_name: str) -> bool:
    """Whether ``toolset`` has a tool whose Strands ``tool_name`` is ``tool_name``."""
    return any(tool.tool_name == tool_name for tool in get_tools(toolset))


def list_toolsets() -> list[str]:
    """Every registered toolset name, in registration order."""
    return list(_REGISTRY)


def list_handlers() -> dict[str, list[str]]:
    """``{toolset: [tool_name, ...]}`` for every registered toolset."""
    return {name: [tool.tool_name for tool in tools] for name, tools in _REGISTRY.items()}
