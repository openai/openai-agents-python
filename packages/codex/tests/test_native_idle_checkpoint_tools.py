"""Settled checkpoints retain history while fresh turns use current Python tools."""

import json
from dataclasses import replace

import pytest

from agents import Agent, FunctionTool, function_tool
from tests._asyncio import timeout
from tests.test_native_agent import function_declarations
from tests.test_native_agent_checkpoints import call, message, peer
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize(
    "change", ["description", "strict", "schema", "addition", "removal", "callback"]
)
async def test_idle_checkpoint_uses_current_python_tools(change):
    effects = []

    @function_tool(name="lookup", description="Original lookup", strict=True)
    def original(key: str) -> str:
        effects.append("original:" + key)
        return "original:" + key

    @function_tool(name="lookup", description="Original lookup", strict=True)
    def current(key: str) -> str:
        effects.append("current:" + key)
        return "current:" + key

    @function_tool(name="lookup", description="Original lookup", strict=True)
    def changed_schema(value: int) -> str:
        effects.append(f"current:{value}")
        return f"current:{value}"

    @function_tool(name="added", description="New lookup", strict=True)
    def added(key: str) -> str:
        effects.append("added:" + key)
        return "added:" + key

    current_tools = [current]
    selected_name, arguments = "lookup", {"key": "fresh"}
    if change == "description":
        current_tools = [replace(current, description="Current lookup")]
    elif change == "strict":
        current_tools = [replace(current, strict=False)]
    elif change == "schema":
        current_tools = [changed_schema]
        arguments = {"value": 7}
    elif change == "addition":
        current_tools.append(added)
        selected_name = "added"
    elif change == "removal":
        current_tools = []

    async with timeout(30), peer() as model:
        agent = Agent(
            instructions="Use the available tools.", tools=[original], provider=model.provider()
        )
        if change == "removal":
            previous = call("lookup", "prior-lookup")
            previous["arguments"] = json.dumps({"key": "saved"})
            model.replies.append(previous)
        model.replies.append(message("saved answer"))
        async with agent.session() as session:
            await (await session.submit("remember this conversation")).get_result()
            saved = await session.save_state()
        fresh_request_index = len(model.requests)
        changed = replace(agent, tools=current_tools)
        if change != "removal":
            request = call(selected_name, "fresh-call")
            request["arguments"] = json.dumps(arguments)
            model.replies.append(request)
        model.replies.append(message("current answer"))
        async with changed.session(state=saved) as restored:
            assert restored.id == saved.source_session_id
            assert (
                await (await restored.submit("continue with current tools")).get_result()
            ).final_output == "current answer"
            saved_again = await restored.save_state()
        assert all(
            record in saved_again.history
            for record in saved.history
            if record["type"] == "response_item"
        )
        wire = model.requests[fresh_request_index]
        assert "remember this conversation" in json.dumps(wire)
        declarations = {tool["name"]: tool for tool in function_declarations(wire)}
        assert set(declarations) == {tool.name for tool in current_tools}
        if change == "description":
            assert declarations["lookup"]["description"] == "Current lookup"
        if change == "strict":
            assert declarations["lookup"].get("strict", False) is False
        if change == "schema":
            assert set(declarations["lookup"]["parameters"]["properties"]) == {"value"}
        if change == "removal":
            old_output = next(
                item
                for item in wire["input"]
                if item["type"] == "function_call_output" and item["call_id"] == "prior-lookup"
            )
            assert "original:saved" in json.dumps(old_output["output"])
        assert effects == (
            ["original:saved"]
            if change == "removal"
            else [
                "added:fresh"
                if change == "addition"
                else "current:7"
                if change == "schema"
                else "current:fresh"
            ]
        )
        model.replies.append(message("restored twice"))
        async with changed.session(state=saved_again) as restored_again:
            assert (
                await (await restored_again.submit("again")).get_result()
            ).final_output == "restored twice"
            twice = await restored_again.save_state()
        assert twice.managed_tool_specs == saved_again.managed_tool_specs


@pytest.mark.parametrize("change", ["schema", "removal"])
async def test_idle_checkpoint_keeps_explicit_defaults_when_tools_change(change):
    effects = []

    @function_tool(name="lookup")
    def original(key: str) -> str:
        pytest.fail(f"The retired callback ran for {key}")

    @function_tool(name="lookup")
    def current(value: int) -> str:
        effects.append(value)
        return str(value)

    @function_tool
    def other() -> str:
        return "not a default"

    async with timeout(30), peer() as model:
        agent = Agent(
            instructions="Use selected tools.", tools=[original, other], provider=model.provider()
        )
        async with agent.session() as session:
            await session.update(tools=[original])
            saved = await session.save_state()
        changed = replace(agent, tools=[current, other] if change == "schema" else [other])
        if change == "schema":
            request = call("lookup", "current-lookup")
            request["arguments"] = '{"value": 7}'
            model.replies.append(request)
        model.replies.append(message("restored"))
        async with changed.session(state=saved) as restored:
            assert (
                await (await restored.submit("Continue with the saved selection")).get_result()
            ).final_output == "restored"
        declarations = {item["name"]: item for item in function_declarations(model.requests[0])}
        if change == "schema":
            assert set(declarations) == {"lookup"}
            assert set(declarations["lookup"]["parameters"]["properties"]) == {"value"}
            assert effects == [7]
        else:
            assert not declarations
            assert not effects


async def test_idle_checkpoint_rejects_unknown_saved_tool_fields():
    @function_tool
    def lookup() -> str:
        return "current"

    async with timeout(30), peer() as model:
        agent = Agent(instructions="Answer.", tools=[lookup], provider=model.provider())
        async with agent.session() as session:
            saved = await session.save_state()
        raw = json.loads(saved.to_json())
        raw["managed_tool_specs"][0]["untrusted_authority"] = True
        changed = replace(agent, tools=[])
        with pytest.raises(RuntimeError, match="checkpoint|saved.*declarations"):
            async with changed.session(state=type(saved).from_json(json.dumps(raw))):
                pytest.fail("Unknown saved authority was accepted")
        assert not model.requests


@pytest.mark.parametrize("change", ["description", "strict", "removal"])
async def test_pending_checkpoint_keeps_exact_tool_declarations(change):
    lookup = FunctionTool(
        name="lookup", description="Read original", input_type=LookupArgs, output_type=str
    )
    changed_tools = (
        []
        if change == "removal"
        else [replace(lookup, description="Read current")]
        if change == "description"
        else [replace(lookup, strict=not lookup.strict)]
    )
    async with timeout(30), peer() as model:
        agent = Agent(instructions="Read.", tools=[lookup], provider=model.provider())
        request = call("lookup", "pending-lookup")
        request["arguments"] = json.dumps({"key": "pending"})
        model.replies.append(request)
        async with agent.session() as session:
            await session.submit("read")
            await next_request(session.interactions(), lookup)
            saved = await session.suspend()
        with pytest.raises((ValueError, RuntimeError)):
            async with replace(agent, tools=changed_tools).session(state=saved):
                pytest.fail("Pending call accepted changed declarations")
        assert len(model.requests) == 1
