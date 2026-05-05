import asyncio

import pytest
from mcp.types import Tool as MCPTool

from agents import Agent, RunContextWrapper, Runner

from ..fake_model import FakeModel
from ..test_responses import get_function_tool_call, get_text_message
from ..utils.hitl import queue_function_call_and_text, resume_after_first_approval
from .helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_mcp_require_approval_pauses_and_resumes():
    """MCP servers should honor require_approval for non-hosted tools."""

    server = FakeMCPServer(require_approval="always")
    server.add_tool("add", {"type": "object", "properties": {}})

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, mcp_servers=[server])

    queue_function_call_and_text(
        model,
        get_function_tool_call("add", "{}"),
        followup=[get_text_message("done")],
    )

    first = await Runner.run(agent, "call add")

    assert first.interruptions, "MCP tool should request approval"
    assert first.interruptions[0].tool_name == "add"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)

    assert not resumed.interruptions
    assert server.tool_calls == ["add"]
    assert resumed.final_output == "done"


@pytest.mark.asyncio
async def test_mcp_require_approval_tool_lists():
    """TS-style requireApproval toolNames should map to needs_approval."""

    require_approval: dict[str, object] = {
        "always": {"tool_names": ["add"]},
        "never": {"tool_names": ["noop"]},
    }
    server = FakeMCPServer(require_approval=require_approval)
    server.add_tool("add", {"type": "object", "properties": {}})

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, mcp_servers=[server])

    queue_function_call_and_text(
        model,
        get_function_tool_call("add", "{}"),
        followup=[get_text_message("done")],
    )

    first = await Runner.run(agent, "call add")
    assert first.interruptions, "add should require approval via require_approval toolNames"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)
    assert resumed.final_output == "done"
    assert server.tool_calls == ["add"]


@pytest.mark.asyncio
async def test_mcp_require_approval_tool_mapping():
    """Tool-name require_approval mappings should map to needs_approval."""

    require_approval = {"add": "always", "noop": "never"}
    server = FakeMCPServer(require_approval=require_approval)
    server.add_tool("add", {"type": "object", "properties": {}})

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, mcp_servers=[server])

    queue_function_call_and_text(
        model,
        get_function_tool_call("add", "{}"),
        followup=[get_text_message("done")],
    )

    first = await Runner.run(agent, "call add")
    assert first.interruptions, "add should require approval via require_approval mapping"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)
    assert resumed.final_output == "done"
    assert server.tool_calls == ["add"]


@pytest.mark.asyncio
async def test_mcp_require_approval_mapping_allows_policy_keyword_tool_names():
    """Tool-name mappings should treat literal 'always'/'never' as tool names."""

    require_approval = {"always": "always", "never": "never"}
    server = FakeMCPServer(require_approval=require_approval)
    server.add_tool("always", {"type": "object", "properties": {}})
    server.add_tool("never", {"type": "object", "properties": {}})

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, mcp_servers=[server])

    queue_function_call_and_text(
        model,
        get_function_tool_call("always", "{}"),
        followup=[get_text_message("done")],
    )

    first = await Runner.run(agent, "call always")
    assert first.interruptions, "tool named 'always' should require approval"
    assert first.interruptions[0].tool_name == "always"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)
    assert resumed.final_output == "done"

    queue_function_call_and_text(
        model,
        get_function_tool_call("never", "{}"),
        followup=[get_text_message("done")],
    )

    second = await Runner.run(agent, "call never")
    assert not second.interruptions, "tool named 'never' should not require approval"


@pytest.mark.asyncio
async def test_mcp_require_approval_callable_can_allow_and_block_by_tool_name():
    """Callable policies should decide approval dynamically for each MCP tool."""

    seen: list[str] = []

    def require_approval(
        _run_context: RunContextWrapper[object | None],
        _agent: Agent,
        tool: MCPTool,
    ) -> bool:
        seen.append(tool.name)
        return tool.name == "guarded"

    server = FakeMCPServer(require_approval=require_approval)
    server.add_tool("guarded", {"type": "object", "properties": {}})
    server.add_tool("safe", {"type": "object", "properties": {}})

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, mcp_servers=[server])

    queue_function_call_and_text(
        model,
        get_function_tool_call("guarded", "{}"),
        followup=[get_text_message("guarded done")],
    )
    first = await Runner.run(agent, "call guarded")
    assert first.interruptions, "guarded should require approval via callable policy"
    assert first.interruptions[0].tool_name == "guarded"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)
    assert resumed.final_output == "guarded done"

    queue_function_call_and_text(
        model,
        get_function_tool_call("safe", "{}"),
        followup=[get_text_message("safe done")],
    )
    second = await Runner.run(agent, "call safe")
    assert not second.interruptions, "safe should bypass approval via callable policy"
    assert second.final_output == "safe done"

    assert seen == ["guarded", "guarded", "safe"]


@pytest.mark.asyncio
async def test_mcp_require_approval_async_callable_uses_run_context():
    """Async callable policies should receive the run context and be awaited."""

    seen_contexts: list[object | None] = []

    async def require_approval(
        run_context: RunContextWrapper[dict[str, bool] | None],
        _agent: Agent,
        _tool,
    ) -> bool:
        seen_contexts.append(run_context.context)
        await asyncio.sleep(0)
        return bool(run_context.context and run_context.context.get("needs_approval"))

    server = FakeMCPServer(require_approval=require_approval)
    server.add_tool("conditional", {"type": "object", "properties": {}})

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, mcp_servers=[server])

    queue_function_call_and_text(
        model,
        get_function_tool_call("conditional", "{}"),
        followup=[get_text_message("approved path")],
    )
    first = await Runner.run(agent, "call conditional", context={"needs_approval": True})
    assert first.interruptions, "run context should be able to trigger approval"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)
    assert resumed.final_output == "approved path"

    queue_function_call_and_text(
        model,
        get_function_tool_call("conditional", "{}"),
        followup=[get_text_message("no approval path")],
    )
    second = await Runner.run(agent, "call conditional", context={"needs_approval": False})
    assert not second.interruptions, "run context should be able to skip approval"
    assert second.final_output == "no approval path"

    assert seen_contexts == [
        {"needs_approval": True},
        {"needs_approval": True},
        {"needs_approval": False},
    ]
