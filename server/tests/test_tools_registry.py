"""The toolset registry: what ``RunRequest.toolset`` can name and what it gets."""

from __future__ import annotations

from nimbus.tools import registry

FRAUD_TOOL_NAMES = [
    "freeze_account",
    "flag_suspicious_transaction",
    "create_fraud_alert",
    "update_risk_profile",
]


def test_the_example_toolset_is_registered_under_its_name():
    assert registry.list_toolsets() == ["fraud-detection"]


def test_get_tools_returns_strands_tools_in_registration_order():
    tools = registry.get_tools("fraud-detection")
    assert [tool.tool_name for tool in tools] == FRAUD_TOOL_NAMES
    # Every entry is a real Strands tool with a spec the model can be given.
    for tool in tools:
        assert tool.tool_spec["name"] == tool.tool_name
        assert tool.tool_spec["inputSchema"]["json"]["type"] == "object"


def test_get_tools_returns_a_fresh_list_each_call():
    first = registry.get_tools("fraud-detection")
    first.clear()
    assert [tool.tool_name for tool in registry.get_tools("fraud-detection")] == FRAUD_TOOL_NAMES


def test_unknown_toolset_is_empty_not_an_error():
    assert registry.get_tools("shipping-logistics") == []
    assert registry.get_tools("") == []


def test_has_handler_matches_on_the_strands_tool_name():
    assert registry.has_handler("fraud-detection", "freeze_account")
    assert not registry.has_handler("fraud-detection", "freezeAccount")
    assert not registry.has_handler("fraud-detection", "getCarrierStatus")
    assert not registry.has_handler("nope", "freeze_account")


def test_list_handlers_is_the_get_tools_shape():
    assert registry.list_handlers() == {"fraud-detection": FRAUD_TOOL_NAMES}


async def test_get_tools_advertises_every_toolset_with_its_tool_names(client):
    response = await client.get("/api/v1/tools")

    assert response.status_code == 200
    assert response.json() == {"toolsets": [{"name": "fraud-detection", "tools": FRAUD_TOOL_NAMES}]}
