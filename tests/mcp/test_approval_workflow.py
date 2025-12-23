import json
import logging

import pytest
from mcp.types import Tool as MCPTool

from agents import RunContextWrapper
from agents.mcp import MCPUtil
from agents.tool import MCPToolApprovalFunctionResult, MCPToolApprovalRequest

from .helpers import FakeMCPServer


class ApprovalFakeMCPServer(FakeMCPServer):
    """Fake MCP server that supports approval workflows."""

    def __init__(
        self,
        tools: list[MCPTool] | None = None,
        tool_filter=None,
        server_name: str = "fake_mcp_server",
        on_approval_request=None,
        require_approval: str | dict[str, str] = "never",
    ):
        super().__init__(tools=tools, tool_filter=tool_filter, server_name=server_name)
        self.on_approval_request = on_approval_request
        self.require_approval = require_approval

    def _should_require_approval(self, tool_name: str) -> bool:
        """Check if approval is required for a specific tool."""
        if self.require_approval == "never":
            return False
        if self.require_approval == "always":
            return True
        if isinstance(self.require_approval, dict):
            return self.require_approval.get(tool_name, "never") == "always"
        return False


@pytest.mark.asyncio
async def test_approval_not_required():
    """Test that tools execute normally when approval is not required."""
    server = ApprovalFakeMCPServer(require_approval="never")
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, '{"arg": "value"}')

    # Tool should execute normally
    assert "result_test_tool" in result
    assert len(server.tool_calls) == 1
    assert server.tool_calls[0] == "test_tool"


@pytest.mark.asyncio
async def test_approval_required_and_approved():
    """Test that tools execute when approval is required and granted."""
    approval_calls = []

    def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        approval_calls.append(request)
        return {"approve": True}

    server = ApprovalFakeMCPServer(require_approval="always", on_approval_request=approval_callback)
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, '{"arg": "value"}')

    # Approval callback should be called
    assert len(approval_calls) == 1
    assert approval_calls[0].data.name == "test_tool"
    assert approval_calls[0].data.arguments == '{"arg": "value"}'
    assert approval_calls[0].data.server_label == "fake_mcp_server"

    # Tool should execute after approval
    assert "result_test_tool" in result
    assert len(server.tool_calls) == 1
    assert server.tool_calls[0] == "test_tool"


@pytest.mark.asyncio
async def test_approval_required_and_rejected():
    """Test that tools return rejection result when approval is denied."""
    approval_calls = []

    def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        approval_calls.append(request)
        return {"approve": False, "reason": "User denied"}

    server = ApprovalFakeMCPServer(require_approval="always", on_approval_request=approval_callback)
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, '{"arg": "value"}')

    # Approval callback should be called
    assert len(approval_calls) == 1

    # Should return rejection result as JSON, not raise error
    result_dict = json.loads(result)
    assert result_dict["rejected"] is True
    assert "User denied" in result_dict["error"]
    assert "Tool execution was rejected" in result_dict["error"]

    # Tool should NOT execute
    assert len(server.tool_calls) == 0


@pytest.mark.asyncio
async def test_approval_rejected_without_reason():
    """Test that rejection works even without a reason."""

    def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        return {"approve": False}

    server = ApprovalFakeMCPServer(require_approval="always", on_approval_request=approval_callback)
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    result_dict = json.loads(result)
    assert result_dict["rejected"] is True
    assert "Tool execution was rejected" in result_dict["error"]

    # Tool should NOT execute
    assert len(server.tool_calls) == 0


@pytest.mark.asyncio
async def test_per_tool_approval_policy():
    """Test that per-tool approval policies work correctly."""
    approval_calls = []

    def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        approval_calls.append(request.data.name)
        # Approve tool1, reject tool2
        if request.data.name == "tool1":
            return {"approve": True}
        return {"approve": False, "reason": "Not allowed"}

    server = ApprovalFakeMCPServer(
        require_approval={"tool1": "always", "tool2": "always", "tool3": "never"},
        on_approval_request=approval_callback,
    )
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})

    ctx = RunContextWrapper(context=None)

    # tool1 requires approval and should be approved
    tool1 = MCPTool(name="tool1", inputSchema={})
    result1 = await MCPUtil.invoke_mcp_tool(server, tool1, ctx, "{}")
    assert "result_tool1" in result1
    assert len(approval_calls) == 1
    assert approval_calls[0] == "tool1"

    # tool2 requires approval and should be rejected
    tool2 = MCPTool(name="tool2", inputSchema={})
    result2 = await MCPUtil.invoke_mcp_tool(server, tool2, ctx, "{}")
    result2_dict = json.loads(result2)
    assert result2_dict["rejected"] is True
    assert len(approval_calls) == 2
    assert approval_calls[1] == "tool2"

    # tool3 doesn't require approval
    tool3 = MCPTool(name="tool3", inputSchema={})
    result3 = await MCPUtil.invoke_mcp_tool(server, tool3, ctx, "{}")
    assert "result_tool3" in result3
    assert len(approval_calls) == 2  # No additional approval call

    # Verify tool execution counts
    assert len(server.tool_calls) == 2  # tool1 and tool3 executed
    assert "tool1" in server.tool_calls
    assert "tool3" in server.tool_calls
    assert "tool2" not in server.tool_calls


@pytest.mark.asyncio
async def test_approval_required_but_no_callback(caplog: pytest.LogCaptureFixture):
    """Test that tools execute with warning when approval required but no callback."""
    caplog.set_level(logging.WARNING)

    server = ApprovalFakeMCPServer(require_approval="always", on_approval_request=None)
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should log warning
    assert "requires approval" in caplog.text.lower()
    assert "no on_approval_request callback" in caplog.text.lower()

    # Tool should still execute (backward compatibility)
    assert "result_test_tool" in result
    assert len(server.tool_calls) == 1


@pytest.mark.asyncio
async def test_async_approval_callback():
    """Test that async approval callbacks work correctly."""
    approval_calls = []

    async def async_approval_callback(
        request: MCPToolApprovalRequest,
    ) -> MCPToolApprovalFunctionResult:
        approval_calls.append(request)
        return {"approve": True}

    server = ApprovalFakeMCPServer(
        require_approval="always", on_approval_request=async_approval_callback
    )
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, '{"arg": "value"}')

    # Approval callback should be called
    assert len(approval_calls) == 1

    # Tool should execute after approval
    assert "result_test_tool" in result
    assert len(server.tool_calls) == 1


@pytest.mark.asyncio
async def test_approval_callback_exception():
    """Test that callback exceptions are handled gracefully."""

    def failing_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        raise ValueError("Callback error")

    server = ApprovalFakeMCPServer(require_approval="always", on_approval_request=failing_callback)
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should return error result, not raise exception
    result_dict = json.loads(result)
    assert result_dict["rejected"] is True
    assert "Error in approval callback" in result_dict["error"]
    assert "Callback error" in result_dict["error"]

    # Tool should NOT execute
    assert len(server.tool_calls) == 0


@pytest.mark.asyncio
async def test_approval_request_structure():
    """Test that approval request has correct structure."""
    approval_calls = []

    def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        approval_calls.append(request)
        # Verify request structure
        assert hasattr(request, "ctx_wrapper")
        assert hasattr(request, "data")
        assert request.data.name == "test_tool"
        assert request.data.server_label == "custom_server"
        assert request.data.type == "mcp_approval_request"
        assert "id" in request.data.model_dump()
        return {"approve": True}

    server = ApprovalFakeMCPServer(
        require_approval="always",
        on_approval_request=approval_callback,
        server_name="custom_server",
    )
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    await MCPUtil.invoke_mcp_tool(server, tool, ctx, '{"key": "value"}')

    assert len(approval_calls) == 1
    # Verify arguments are serialized correctly
    assert approval_calls[0].data.arguments == '{"key": "value"}'


@pytest.mark.asyncio
async def test_approval_with_empty_arguments():
    """Test approval workflow with empty arguments."""
    approval_calls = []

    def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        approval_calls.append(request)
        assert request.data.arguments == "{}"
        return {"approve": True}

    server = ApprovalFakeMCPServer(require_approval="always", on_approval_request=approval_callback)
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")

    assert len(approval_calls) == 1
    assert "result_test_tool" in result
