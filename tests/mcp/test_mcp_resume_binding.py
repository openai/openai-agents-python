import asyncio
import json

import pytest

from agents import Agent, RunContextWrapper, Runner, RunState, UserError, handoff
from agents.decorators import tool
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.testing import ScriptedModel
from agents.tool import ToolOrigin, ToolOriginType

from ..test_responses import get_function_tool_call, get_text_message
from .helpers import FakeMCPServer


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("changed_recipient", [False, True])
async def test_mcp_recipient_survives_absent_tool_during_restore(
    streaming: bool, changed_recipient: bool
):
    original = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    original.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[original, other],
        model=ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    state.approve(first.interruptions[0])
    snapshot = state.to_json()
    original.tools.clear()
    restored = await RunState.from_json(agent, snapshot)
    # Saving while discovery has no candidate must retain the original recipient.
    restored = await RunState.from_json(agent, restored.to_json())
    (other if changed_recipient else original).add_tool("search", {})

    async def resume():
        if streaming:
            result = Runner.run_streamed(agent, restored)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, restored)

    if changed_recipient:
        with pytest.raises(UserError, match="different recipient"):
            await resume()
        assert original.tool_calls == other.tool_calls == []
    else:
        result = await resume()
        assert result.final_output == "done"
        assert original.tool_calls == ["search"]
        assert other.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_mcp_approval_cannot_authorize_handoff(
    streaming: bool, restore: bool, approved: bool
):
    handoff_calls: list[str] = []
    server = FakeMCPServer(require_approval="always")
    server.add_tool("search", {})
    model = ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]])
    agent = Agent(name="test", mcp_servers=[server], model=model)
    delegate = Agent(name="delegate", model=model)
    first = await Runner.run(agent, "search")
    state = first.to_state()
    if approved:
        state.approve(first.interruptions[0])
    else:
        state.reject(first.interruptions[0], rejection_message="MCP request declined")
    agent.handoffs = [
        handoff(
            delegate,
            tool_name_override="search",
            on_handoff=lambda _context: handoff_calls.append("handoff"),
        )
    ]
    if restore:
        state = await RunState.from_json(agent, state.to_json())

    async def resume():
        if streaming:
            result = Runner.run_streamed(agent, state)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, state)

    if approved:
        with pytest.raises(UserError, match="MCP tool call as a handoff"):
            await resume()
    else:
        result = await resume()
        assert result.final_output == "done"
        assert result.last_agent is agent
        assert any(
            getattr(item, "output", None) == "MCP request declined" for item in result.new_items
        )
    assert server.tool_calls == []
    assert handoff_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("listing_change", ["none", "before_restore", "after_restore"])
async def test_serialized_mcp_approval_preserves_recipient(streaming: bool, listing_change: str):
    trusted = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    for server in (trusted, other):
        server.add_tool("search", {"type": "object", "properties": {}})
    arguments = '{"query":"synthetic document"}'
    model = ScriptedModel(
        [
            [get_function_tool_call("mcp_docs__search_15de6fa1", arguments)],
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[trusted, other],
        mcp_config={"include_server_in_tool_names": True},
    )
    if streaming:
        first = Runner.run_streamed(agent, "search")
        async for _ in first.stream_events():
            pass
    else:
        first = await Runner.run(agent, "search")
    assert len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0])
    snapshot = state.to_string()
    payload = json.loads(snapshot)
    assert payload["$schemaVersion"] == "1.18"
    assert payload["last_processed_response"]["mcp_tool_bindings"][
        first.interruptions[0].raw_item.call_id
    ] == [
        "docs",
        "search",
        0,
    ]
    assert trusted.tool_calls == other.tool_calls == []

    restored = (
        await RunState.from_string(agent, snapshot) if listing_change != "before_restore" else None
    )
    if listing_change != "none":
        other.tools.clear()
        other.add_tool("search_15de6fa1", {"type": "object", "properties": {}})
        # The original public name now belongs to the other server's new raw tool.
        names = [tool.name for tool in await agent.get_all_tools(RunContextWrapper(context=None))]
        assert names == ["mcp_docs__search", "mcp_docs__search_15de6fa1"]
        if listing_change == "before_restore":
            restored = await RunState.from_string(agent, snapshot)
        assert restored is not None
        with pytest.raises(UserError, match="different recipient"):
            if streaming:
                resumed = Runner.run_streamed(agent, restored)
                async for _ in resumed.stream_events():
                    pass
            else:
                await Runner.run(agent, restored)
        assert trusted.tool_calls == other.tool_calls == []
    else:
        assert restored is not None
        if streaming:
            resumed = Runner.run_streamed(agent, restored)
            async for _ in resumed.stream_events():
                pass
        else:
            resumed = await Runner.run(agent, restored)
        assert resumed.final_output == "done"
        assert trusted.tool_calls == ["search"]
        assert trusted.tool_results == [f"result_search_{json.dumps(json.loads(arguments))}"]
        assert other.tool_calls == []


@pytest.mark.asyncio
async def test_unprefixed_mcp_resume_rejects_different_server_with_same_raw_tool():
    first_server = FakeMCPServer(server_name="docs", require_approval="always")
    second_server = FakeMCPServer(server_name="docs", require_approval="always")
    first_server.add_tool("search", {})
    agent = Agent(
        name="test",
        model=ScriptedModel([[get_function_tool_call("search", "{}")]]),
        mcp_servers=[first_server, second_server],
    )
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    first_server.tools.clear()
    second_server.add_tool("search", {})
    restored = await RunState.from_json(agent, snapshot)
    with pytest.raises(UserError, match="different recipient"):
        await Runner.run(agent, restored)
    assert first_server.tool_calls == second_server.tool_calls == []


@pytest.mark.asyncio
async def test_legacy_pending_mcp_call_requires_new_run():
    server = FakeMCPServer(require_approval="always")
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        model=ScriptedModel([[get_function_tool_call("search", "{}")]]),
        mcp_servers=[server],
    )
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    for entry in snapshot["context"].pop("function_tool_approvals", []):
        snapshot["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    snapshot["$schemaVersion"] = "1.17"
    del snapshot["last_processed_response"]["mcp_tool_bindings"]
    restored = await RunState.from_json(agent, snapshot)
    with pytest.raises(UserError, match="missing or different recipient binding"):
        await Runner.run(agent, restored)
    assert server.tool_calls == []


@pytest.mark.asyncio
async def test_mcp_resume_rejects_different_raw_tool_on_same_server():
    server = FakeMCPServer(server_name="docs", require_approval="always")
    server.add_tool("search!", {})
    server.add_tool("search?", {})
    model = ScriptedModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
        mcp_config={"include_server_in_tool_names": True},
    )
    original_tools = await agent.get_all_tools(RunContextWrapper(context=None))
    public_name = original_tools[0].name
    model.enqueue([get_function_tool_call(public_name, "{}")])
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    server.tools.pop()
    server.add_tool(public_name.removeprefix("mcp_docs__"), {})
    current_tools = await agent.get_all_tools(RunContextWrapper(context=None))
    assert current_tools[1].name == public_name
    restored = await RunState.from_json(agent, snapshot)
    with pytest.raises(UserError, match="different recipient"):
        await Runner.run(agent, restored)
    assert server.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["1.14", "1.17", "1.18"])
async def test_completed_mcp_sibling_does_not_block_function_approval(schema_version: str):
    function_calls: list[str] = []

    @tool(needs_approval=True)
    async def gated() -> str:
        function_calls.append("gated")
        return "ok"

    server = FakeMCPServer()
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        tools=[gated],
        mcp_servers=[server],
        model=ScriptedModel(
            [
                [
                    get_function_tool_call("search", "{}", call_id="completed_mcp"),
                    get_function_tool_call("gated", "{}", call_id="pending_function"),
                ],
                [get_text_message("done")],
            ]
        ),
    )
    result = await Runner.run(agent, "search and run gated")
    assert server.tool_calls == ["search"]
    assert [item.tool_name for item in result.interruptions] == ["gated"]
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    if schema_version != "1.18":
        for entry in snapshot["context"].pop("function_tool_approvals", []):
            snapshot["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    snapshot["$schemaVersion"] = schema_version
    if schema_version != "1.18":
        snapshot["last_processed_response"].pop("mcp_tool_bindings", None)
    if schema_version == "1.14":
        snapshot["context"].pop("tool_invocations", None)
    restored = await RunState.from_json(agent, snapshot)
    resumed = await Runner.run(agent, restored)
    assert resumed.final_output == "done"
    assert server.tool_calls == ["search"]
    assert function_calls == ["gated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_legacy_mcp_call_missing_during_restore_cannot_rebind(streaming: bool):
    original = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    original.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[original, other],
        model=ScriptedModel(
            [
                [get_function_tool_call("search", '{"query":"synthetic document"}')],
                [get_text_message("done")],
            ]
        ),
    )
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    for entry in snapshot["context"].pop("function_tool_approvals", []):
        snapshot["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    snapshot["$schemaVersion"] = "1.17"
    del snapshot["last_processed_response"]["mcp_tool_bindings"]

    original.tools.clear()
    restored = await RunState.from_json(agent, snapshot)
    other.add_tool("search", {})
    with pytest.raises(UserError, match="missing or different recipient binding"):
        if streaming:
            resumed = Runner.run_streamed(agent, restored)
            async for _ in resumed.stream_events():
                pass
        else:
            await Runner.run(agent, restored)
    assert original.tool_calls == other.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("resume_path", ["in_memory", "before_restore", "after_restore"])
async def test_rejected_mcp_call_continues_after_recipient_changes(
    streaming: bool, resume_path: str
):
    original = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    original.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[original, other],
        model=ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    state.reject(first.interruptions[0], rejection_message="MCP request declined")
    snapshot = state.to_json()
    if resume_path == "after_restore":
        state = await RunState.from_json(agent, snapshot)
    original.tools.clear()
    other.add_tool("search", {})
    if resume_path == "before_restore":
        state = await RunState.from_json(agent, snapshot)

    if streaming:
        result = Runner.run_streamed(agent, state)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, state)
    assert result.final_output == "done"
    assert any(getattr(item, "output", None) == "MCP request declined" for item in result.new_items)
    assert original.tool_calls == other.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_restored_rejection_retains_recipient_when_later_approved(streaming: bool):
    original = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    original.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[original, other],
        model=ScriptedModel([[get_function_tool_call("search", "{}")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    state.reject(first.interruptions[0])
    snapshot = state.to_json()
    original.tools.clear()
    other.add_tool("search", {})
    restored = await RunState.from_json(agent, snapshot)
    # A second snapshot must still identify the original server, even after discovery.
    restored = await RunState.from_json(agent, restored.to_json())
    restored.approve(first.interruptions[0])
    with pytest.raises(UserError, match="different recipient"):
        if streaming:
            result = Runner.run_streamed(agent, restored)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, restored)
    assert original.tool_calls == other.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("approval_policy", ["always", "conditional", "never"])
async def test_mcp_approval_cannot_authorize_local_replacement(
    streaming: bool, restore: bool, approval_policy: str
):
    local_calls: list[str] = []
    approval_checks: list[dict[str, str]] = []

    async def check_approval(_context, arguments, _call_id):
        approval_checks.append(arguments)
        return True

    @tool(
        needs_approval=check_approval
        if approval_policy == "conditional"
        else approval_policy == "always"
    )
    async def search(query: str) -> str:
        local_calls.append(query)
        return "local result"

    server = FakeMCPServer(require_approval="always")
    server.add_tool("search", {})
    call = get_function_tool_call("search", '{"query":"synthetic input"}')
    agent = Agent(
        name="test",
        mcp_servers=[server],
        model=ScriptedModel([[call], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    state.approve(first.interruptions[0])
    agent.tools = [search]
    if restore:
        state = await RunState.from_json(agent, state.to_json())
    with pytest.raises(UserError, match="different recipient"):
        if streaming:
            result = Runner.run_streamed(agent, state)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, state)
    assert local_calls == []
    assert approval_checks == []
    assert server.tool_calls == []

    # A new run selects the intended local recipient and evaluates its own policy.
    agent.model = ScriptedModel([[call], [get_text_message("done")]])
    fresh = await Runner.run(agent, "search")
    if approval_policy != "never":
        assert len(fresh.interruptions) == 1
        assert local_calls == []
        fresh_state = fresh.to_state()
        fresh_state.approve(fresh.interruptions[0])
        fresh = await Runner.run(agent, fresh_state)
    assert fresh.final_output == "done"
    assert local_calls == ["synthetic input"]
    assert approval_checks == (
        [{"query": "synthetic input"}] if approval_policy == "conditional" else []
    )
    assert server.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("restore", [False, True])
async def test_rejected_mcp_call_does_not_invoke_local_replacement(streaming: bool, restore: bool):
    local_calls: list[str] = []

    @tool(needs_approval=True)
    async def search() -> str:
        local_calls.append("search")
        return "local result"

    server = FakeMCPServer(server_name="docs", require_approval="always")
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[server],
        model=ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    expected_origin = ToolOrigin(type=ToolOriginType.MCP, mcp_server_name="docs")
    assert first.interruptions[0].tool_origin == expected_origin
    state = first.to_state()
    state.reject(first.interruptions[0], rejection_message="MCP request declined")
    agent.tools = [search]
    if restore:
        state = await RunState.from_json(agent, state.to_json())
    if streaming:
        result = Runner.run_streamed(agent, state)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, state)
    assert result.final_output == "done"
    call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    output_items = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert len(call_items) == len(output_items) == 1
    assert call_items[0].tool_origin == output_items[0].tool_origin == expected_origin
    assert output_items[0].output == "MCP request declined"
    snapshot = result.to_state().to_json()
    serialized_outputs = [
        item for item in snapshot["generated_items"] if item["type"] == "tool_call_output_item"
    ]
    assert len(serialized_outputs) == 1
    assert serialized_outputs[0]["tool_origin"] == {"type": "mcp", "mcp_server_name": "docs"}
    restored = await RunState.from_json(agent, snapshot)
    restored_outputs = [
        item for item in restored._generated_items if isinstance(item, ToolCallOutputItem)
    ]
    assert len(restored_outputs) == 1
    assert restored_outputs[0].tool_origin == expected_origin
    assert local_calls == server.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("original_mcp", [False, True])
async def test_legacy_approval_cannot_authorize_local_replacement_of_mcp(
    streaming: bool, original_mcp: bool
):
    local_calls: list[str] = []

    @tool(needs_approval=True)
    async def search() -> str:
        local_calls.append("search")
        return "local result"

    server = FakeMCPServer(server_name="docs", require_approval="always")
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        tools=[] if original_mcp else [search],
        mcp_servers=[server] if original_mcp else [],
        model=ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    state.approve(first.interruptions[0])
    snapshot = state.to_json()
    for entry in snapshot["context"].pop("function_tool_approvals", []):
        snapshot["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    snapshot["$schemaVersion"] = "1.17"
    del snapshot["last_processed_response"]["mcp_tool_bindings"]
    agent.tools = [search]
    restored = await RunState.from_json(agent, snapshot)

    async def resume():
        if streaming:
            result = Runner.run_streamed(agent, restored)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, restored)

    if original_mcp:
        with pytest.raises(UserError, match="missing or different recipient binding"):
            await resume()
        assert local_calls == []
    else:
        result = await resume()
        assert result.final_output == "done"
        assert local_calls == ["search"]
    assert server.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("round_trip", [False, True])
async def test_removed_local_override_preserves_original_mcp_recipient(
    streaming: bool, round_trip: bool
):
    local_calls: list[str] = []

    @tool
    async def search() -> str:
        local_calls.append("search")
        return "local result"

    server = FakeMCPServer(server_name="docs", require_approval="always")
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[server],
        model=ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    state.approve(first.interruptions[0])
    agent.tools = [search]
    restored = await RunState.from_json(agent, state.to_json())
    agent.tools = []
    if round_trip:
        restored = await RunState.from_json(agent, restored.to_json())
    if streaming:
        result = Runner.run_streamed(agent, restored)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, restored)
    assert result.final_output == "done"
    assert server.tool_calls == ["search"]
    assert local_calls == []
    # Restoration must not put historical MCP metadata on the application's tool.
    assert search._mcp_tool_binding is None


@pytest.mark.asyncio
@pytest.mark.parametrize("local_override", [False, True])
async def test_cancelled_rejection_preserves_recipient_for_later_approval(local_override: bool):
    local_calls: list[str] = []

    @tool
    async def search() -> str:
        local_calls.append("search")
        return "local result"

    class ListingServer(FakeMCPServer):
        ready: asyncio.Event | None = None

        async def list_tools(self, run_context=None, agent=None):
            tools = await super().list_tools(run_context, agent)
            if self.ready is not None:
                loop = asyncio.get_running_loop()
                # Let discovery and reconciliation finish before requesting cancellation,
                # without starting another tool whose uncommitted execution blocks retry.
                loop.call_soon(loop.call_soon, self.ready.set)
            return tools

    original = FakeMCPServer(server_name="docs", require_approval="always")
    other = ListingServer(server_name="docs", require_approval="always")
    original.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[original, other],
        model=ScriptedModel([[get_function_tool_call("search", "{}")], [get_text_message("done")]]),
    )
    first = await Runner.run(agent, "search")
    state = first.to_state()
    approval = first.interruptions[0]
    state.reject(approval)
    original.tools.clear()
    other.add_tool("search", {})
    if local_override:
        agent.tools = [search]
    state = await RunState.from_json(agent, state.to_json())
    other.ready = asyncio.Event()
    task = asyncio.create_task(Runner.run(agent, state))
    try:
        await asyncio.wait_for(other.ready.wait(), timeout=10)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    assert task.cancelled()
    other.ready = None

    snapshot = state.to_json()
    assert snapshot["last_processed_response"]["mcp_tool_bindings"][approval.raw_item.call_id] == [
        "docs",
        "search",
        0,
    ]
    agent.tools = []
    restored = await RunState.from_json(agent, snapshot)
    restored.approve(approval)
    with pytest.raises(UserError, match="different recipient"):
        await Runner.run(agent, restored)
    assert original.tool_calls == other.tool_calls == []
    assert local_calls == []
    assert search._mcp_tool_binding is None
