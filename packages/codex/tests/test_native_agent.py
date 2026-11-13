"""Run the native-first public API against a synthetic Responses peer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, ValidationError

import agents
from agents import (
    Agent,
    FunctionTool,
    NativeProvider,
    Session,
    StaleTurnError,
    ToolContext,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


def test_session_exposes_only_the_core_workflow_methods() -> None:
    assert {
        name
        for name in dir(Session)
        if not name.startswith("_") and callable(getattr(Session, name))
    } == {
        "aclose",
        "respond",
        "submit",
        "save_state",
        "checkpoint_status",
        "checklist",
        "goal",
        "set_paused_goal",
        "fork",
        "fork_stored",
        "stored_sessions",
        "stored_turns",
        "stored_items",
        "search_history",
        "revert_stored",
        "delete_stored",
        "set_stored_name",
        "items",
        "history_snapshot",
        "pop_history",
        "transact_history",
        "turns",
        "rate_limits",
        "plan_usage",
        "cancel_all",
        "suspend",
        "suspend_workspace",
        "flush_memory",
        "subagents",
        "model_context",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "list_mcp_prompts",
        "get_mcp_prompt",
        "compact",
        "update",
        "get_active_agent",
        "build_realtime_sip_config",
        "realtime",
        "events",
        "interactions",
        "pending_interactions",
    }
    for name in ("GoalState", "PlanningTools"):
        assert name not in agents.__all__
        assert not hasattr(agents, name)
    for name in ("Checklist", "ChecklistState", "ChecklistStep", "EnvironmentSource"):
        assert name in agents.__all__
        assert hasattr(agents, name)


@dataclass
class Application:
    tenant: str = "tenant-a"
    calls: list[ToolContext[Application]] = field(default_factory=list)


class Answer(BaseModel):
    answer: str


def provider(responses):
    return NativeProvider(base_url=responses.url, api_key="synthetic-native-agent-key")


def function_declarations(request):
    declarations = list(request.get("tools", []))
    for item in request["input"]:
        if item.get("type") == "additional_tools":
            declarations += item["tools"]
    if len(declarations) == 1 and declarations[0]["type"] == "namespace":
        assert declarations[0]["name"] == "functions"
        declarations = declarations[0]["tools"]
    return declarations


async def test_native_request_identifies_python_sdk_and_installed_version(responses):
    from agents.version import __version__

    responses.respond(assistant_output("identified"))
    async with Agent(
        instructions="Identify the client.", provider=provider(responses)
    ).session() as session:
        result = await (await session.submit("identify client")).get_result()
        assert result.final_output == "identified"

    [headers] = responses.request_headers
    assert headers["originator"] == "openai_agents_python"
    assert headers["user-agent"].split(" ", 1)[0] == f"openai_agents_python/{__version__}"


async def test_native_tool_context_output_history_and_exact_wait(responses):
    application = Application()

    @function_tool
    async def lookup(context: ToolContext[Application], key: str) -> str:
        """Read a tenant record."""
        context.context.calls.append(context)
        return f"{context.context.tenant}:{key}"

    assert isinstance(lookup, FunctionTool)
    responses.respond(function_call("lookup", {"key": "record-a"}, "lookup-a"))
    responses.respond(assistant_output('{"answer":"first-answer"}'))
    responses.respond(assistant_output('{"answer":"second-answer"}'))
    agent = Agent(
        instructions="Answer using the selected record tool.",
        tools=[lookup],
        provider=provider(responses),
        model_settings={
            "reasoning_effort": "low",
            "reasoning_summary": "concise",
            "service_tier": "flex",
        },
        output_type=Answer,
    )
    async with agent.session(context=application) as session:
        first = await session.submit("first-input")
        result, same_result = await asyncio.gather((first).get_result(), (first).get_result())
        assert same_result is result
        assert result.turn is first
        assert result.final_output == Answer(answer="first-answer")
        [call] = application.calls
        assert call.context is application
        assert "tenant-a" not in repr(call)
        assert call.submission is first
        assert call.call_id == "lookup-a"

        request = responses.requests[0]
        assert "You are an AI agent." in str(request["input"])
        assert "Answer using the selected record tool." in str(request["input"])
        developer_text = "\n".join(
            content["text"]
            for item in request["input"]
            if item.get("role") == "developer"
            for content in item.get("content", [])
            if "text" in content
        )
        assert "Answer using the selected record tool." in developer_text
        assert developer_text.count("Answer using the selected record tool.") == 1
        assert not any(
            "Answer using the selected record tool." in str(item)
            for item in request["input"]
            if item.get("role") == "user"
        )
        assert request["reasoning"]["effort"] == "low"
        assert request["reasoning"]["summary"] == "concise"
        assert request["service_tier"] == "flex"
        [declaration] = function_declarations(request)
        assert declaration["name"] == "lookup"
        assert set(declaration["parameters"]["properties"]) == {"key"}
        assert request["text"]["format"]["schema"]["properties"]["answer"]["type"] == "string"
        assert "_agents_sdk" not in str(responses.requests)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_second(event):
            if event["type"] == "response.created":
                entered.set()
                await release.wait()

        responses.before_event = hold_second
        second = await session.submit("second-input")
        await asyncio.wait_for(entered.wait(), 10)
        try:
            await session._cancel(first)
            assert await (first).get_result() is result
        finally:
            release.set()
        assert (await (second).get_result()).final_output == Answer(answer="second-answer")
        assert await (first).get_result() is result
        items = [entry async for entry in session.items()]
        assert any(
            item["type"] == "function_call" and item["call_id"] == "lookup-a" for item in items
        )
        assert any(
            item["type"] == "function_call_output" and item["call_id"] == "lookup-a"
            for item in items
        )
        assert len([entry async for entry in session.turns()]) == 2
    assert await (first).get_result() is result
    with pytest.raises(RuntimeError, match="closed"):
        await session.submit("after-close")


async def test_waiter_cancellation_and_overlapping_input_do_not_change_work(responses):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    @function_tool
    async def slow_lookup(key: str) -> str:
        calls.append(key)
        entered.set()
        await release.wait()
        return "tool-result"

    responses.respond(function_call("slow_lookup", {"key": "record-a"}, "slow-a"))
    responses.respond(assistant_output("completed"))
    agent = Agent(
        instructions="Read the record.", tools=[slow_lookup], provider=provider(responses)
    )
    async with agent.session() as session:
        handle = await session.submit("first-input")
        await asyncio.wait_for(entered.wait(), 10)
        waiter = asyncio.create_task((handle).get_result())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        try:
            await handle.steer("steering-input")
            assert calls == ["record-a"]
            assert len(responses.requests) == 1
        finally:
            release.set()
        assert (await (handle).get_result()).final_output == "completed"
        assert "steering-input" in str(responses.requests)


async def test_cancel_joins_callback_cleanup_and_session_can_continue(responses):
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()

    @function_tool
    async def pending() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
        return "unreachable"

    responses.respond(function_call("pending", {}, "pending-a"))
    responses.respond(assistant_output("after-cancel"))
    agent = Agent(
        instructions="Use the selected tool.", tools=[pending], provider=provider(responses)
    )
    async with agent.session() as session:
        first = await session.submit("first-input")
        await asyncio.wait_for(entered.wait(), 10)
        stopping = asyncio.create_task(first.cancel())
        await asyncio.wait_for(cleanup_started.wait(), 10)
        try:
            assert not cleaned.is_set()
            assert not stopping.done()
            with pytest.raises(RuntimeError, match="finishing root interruption"):
                await session.submit("during-cleanup")
        finally:
            cleanup_release.set()
        await stopping
        assert cleaned.is_set()
        with pytest.raises(TurnStoppedError):
            await (first).get_result()
        second = await session.submit("continue")
        assert (await (second).get_result()).final_output == "after-cancel"


async def test_foreign_handles_and_callback_self_close_fail_without_deadlock(responses):
    current = None

    @function_tool
    async def try_close() -> str:
        assert current is not None
        with pytest.raises(RuntimeError, match="own Session"):
            await current._aclose()
        return "kept-open"

    responses.respond(function_call("try_close", {}, "self-close"))
    responses.respond(assistant_output("completed"))
    agent = Agent(
        instructions="Use the selected tool.", tools=[try_close], provider=provider(responses)
    )
    async with agent.session() as session:
        current = session
        handle = await session.submit("first-input")
        assert (await (handle).get_result()).final_output == "completed"
        async with agent.session() as foreign:
            assert await handle.get_result() is await handle.get_result()
            with pytest.raises(StaleTurnError, match="different Session"):
                await foreign._wait(handle)
            with pytest.raises(StaleTurnError, match="different Session"):
                await foreign._cancel(handle)


async def test_one_shot_uses_native_path_and_sync_rejects_active_loop(responses):
    responses.respond(assistant_output('{"answer":"one-shot"}'))
    agent = Agent(
        instructions="Answer the request.", provider=provider(responses), output_type=Answer
    )
    assert (await agent.run("input")).final_output == Answer(answer="one-shot")
    with pytest.raises(RuntimeError, match="await agent.run"):
        agent.run_sync("input")


def test_provider_credentials_are_not_in_representations():
    value = NativeProvider(
        base_url="https://example.invalid/private?token=secret", api_key="secret-key"
    )
    assert "secret" not in repr(value)


def test_keyword_only_context_fails_at_declaration():
    async def lookup(*, context: ToolContext[Application], key: str) -> str:
        return context.context.tenant + key

    with pytest.raises(TypeError, match="ToolContext must be the first positional parameter"):
        function_tool(lookup)


async def test_invalid_input_does_not_retire_session(responses):
    responses.respond(assistant_output("valid"))
    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        with pytest.raises(TypeError, match="input must be a string"):
            await session.submit(17)
        assert not responses.requests
        handle = await session.submit("valid-input")
        assert (await (handle).get_result()).final_output == "valid"


async def test_foreign_event_loop_fails_before_native_input(responses):
    responses.respond(assistant_output("owner-loop"))
    async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
        with pytest.raises(RuntimeError, match="event loop where it opened"):
            await asyncio.to_thread(lambda: asyncio.run(session.submit("foreign-loop")))
        assert not responses.requests
        handle = await session.submit("owner-loop-input")
        assert (await (handle).get_result()).final_output == "owner-loop"


@pytest.mark.parametrize("name", ["base_url", "api_key"])
def test_explicit_empty_provider_fields_fail_at_declaration(name):
    with pytest.raises(ValueError, match=f"{name} must be a non-empty string"):
        NativeProvider(**{name: ""})


@pytest.mark.parametrize("key", ["record-a", "x"])
async def test_annotated_metadata_keeps_context_injected_and_validates_arguments(responses, key):
    application = Application()
    effects = []

    @function_tool
    async def lookup(
        context: Annotated[ToolContext[Application], "injected"],
        key: Annotated[str, Field(description="Three-character record key", min_length=3)],
    ) -> str:
        effects.append((context.context, key))
        return key

    responses.respond(function_call("lookup", {"key": key}, "annotated-call"))
    responses.respond(assistant_output("finished"))
    agent = Agent(instructions="Use the record tool.", tools=[lookup], provider=provider(responses))
    async with agent.session(context=application) as session:
        handle = await session.submit("Read the record.")
        if key == "x":
            with pytest.raises(ValidationError, match="at least 3 characters"):
                await (handle).get_result()
            assert effects == []
        else:
            assert (await (handle).get_result()).final_output == "finished"
            assert effects == [(application, key)]
        [declaration] = function_declarations(responses.requests[0])
        assert set(declaration["parameters"]["properties"]) == {"key"}
        parameter = declaration["parameters"]["properties"]["key"]
        # Core currently emits its supported schema subset. Python retains
        # Annotated constraints and validates them before the callback.
        assert parameter["description"] == "Three-character record key"


@pytest.mark.parametrize("has_default", [False, True], ids=["required", "default"])
def test_underscore_parameter_fails_at_tool_declaration(has_default):
    if has_default:

        async def read_document(_id: str = "fallback") -> str:
            return _id

    else:

        async def read_document(_id: str) -> str:
            return _id

    with pytest.raises(TypeError, match="_id"):
        function_tool(read_document)


async def test_underscore_wire_alias_preserves_python_parameter_and_native_schema(responses):
    calls = []

    @function_tool
    async def read_document(document_id: Annotated[str, Field(alias="_id")]) -> str:
        calls.append(document_id)
        return f"read:{document_id}"

    responses.respond(function_call("read_document", {"_id": "doc-a"}, "aliased-document"))
    responses.respond(assistant_output("document read"))
    agent = Agent(
        instructions="Read the requested document.",
        tools=[read_document],
        provider=provider(responses),
    )
    async with timeout(30), agent.session() as session:
        handle = await session.submit("Read doc-a.")
        assert (await (handle).get_result()).final_output == "document read"
        assert calls == ["doc-a"]
        [declaration] = function_declarations(responses.requests[0])
        assert set(declaration["parameters"]["properties"]) == {"_id"}
        assert declaration["parameters"]["required"] == ["_id"]
        assert len(responses.requests) == 2
