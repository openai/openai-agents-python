import pytest

from agents import Agent, Runner

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
