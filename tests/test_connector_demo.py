from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_connector_package_demo_verifies_end_to_end() -> None:
    from examples.connectors import package_demo

    summary = await package_demo.verify_connector_demo()

    assert summary["direct_tool_result"] == "discount=25.00"
    assert summary["mcp_tool_result"] == "order demo_order_1001: fulfilled"
    assert summary["package_registry_source"] == "unified_plugins_demo"
    assert summary["hosted_connector_label"] == "calendar"
    assert "apply_discount" in summary["agent_tool_names"]
    assert "mcp_orders__lookup_order" in summary["agent_tool_names"]
