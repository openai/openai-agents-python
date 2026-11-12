"""Exercise custom string declarations through the native Responses route."""

import asyncio
import json
from pathlib import Path

import pytest

from agents import Agent, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request, provider

RAW_INPUT = " \tSELECT raw;  "
GRAMMAR = r"^\s*SELECT.*$"


def custom_call():
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "custom_tool_call",
            "name": "raw_editor",
            "call_id": "raw",
            "input": RAW_INPUT,
        },
    }


@pytest.mark.parametrize(
    ("grammar", "decision", "use_lite"),
    [(None, None, False), (GRAMMAR, True, False), (None, False, True), (GRAMMAR, None, True)],
)
async def test_native_custom_string_tool(responses, grammar, decision, use_lite):
    calls = []

    @function_tool(needs_approval=decision is not None)
    def raw_editor(source: str) -> str:
        calls.append(source)
        return source.upper()

    tool = raw_editor.as_custom(grammar=grammar, syntax="regex")
    responses.respond(custom_call())
    responses.respond(assistant_output("done"))
    model_options = {}
    if not use_lite:
        catalog_path = Path(__file__).parent / "fixtures" / "request-control-models.json"
        model_options["model"] = next(
            entry["slug"]
            for entry in json.loads(catalog_path.read_text())["models"]
            if entry.get("supported_in_api")
            and not entry.get("use_responses_lite")
            and entry.get("tool_mode") != "code_mode_only"
        )

    async with (
        timeout(20),
        Agent(
            instructions="Use raw_editor.",
            tools=[tool],
            provider=provider(responses),
            **model_options,
        ).session() as session,
    ):
        turn = await session.submit("begin")
        if decision is not None:
            request = await next_request(session.interactions(), tool)
            assert request.effective_arguments.source == RAW_INPUT
            assert calls == []
            if decision:
                await request.approve()
            else:
                await request.reject("custom tool denied")
        assert (await turn.get_result()).final_output == "done"

    request = responses.requests[0]
    offered = (
        [
            tool
            for item in request["input"]
            if item.get("type") == "additional_tools"
            for namespace in item["tools"]
            if namespace.get("type") == "namespace" and namespace.get("name") == "functions"
            for tool in namespace["tools"]
        ]
        if use_lite
        else request["tools"]
    )
    wire_tool = next(tool for tool in offered if tool.get("name") == "raw_editor")
    assert wire_tool["type"] == "custom"
    assert wire_tool["format"] == (
        {"type": "text"}
        if grammar is None
        else {"type": "grammar", "syntax": "regex", "definition": grammar}
    )
    assert calls == ([] if decision is False else [RAW_INPUT])
    output = tool_output(responses.requests[1], "raw", "custom_tool_call_output")
    assert output == ("custom tool denied" if decision is False else RAW_INPUT.upper())


async def test_native_custom_string_tool_refuses_function_payload(responses):
    calls = []

    @function_tool
    def raw_editor(source: str) -> str:
        calls.append(source)
        return source

    responses.respond(function_call("raw_editor", {"source": RAW_INPUT}, "wrong-kind"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use raw_editor.",
            tools=[raw_editor.as_custom()],
            provider=provider(responses),
        ).session() as session,
    ):
        assert (await (await session.submit("begin")).get_result()).final_output == "done"
    assert calls == []
    assert "direct provider call payload" in tool_output(responses.requests[1], "wrong-kind")


async def test_native_custom_string_tool_preserves_finish_turn(responses):
    @function_tool(finish_turn=True)
    def raw_editor(source: str) -> str:
        return source.upper()

    responses.respond(custom_call())
    responses.respond(assistant_output("the model continued"))
    agent = Agent(
        instructions="Use raw_editor.",
        tools=[raw_editor.as_custom()],
        provider=provider(responses),
    )
    async with timeout(20), agent.session() as session:
        completed = await (await session.submit("begin")).get_result()
    assert completed.final_output == RAW_INPUT.upper()
    assert completed.tool_call is not None and completed.tool_call.call_id == "raw"
    assert len(responses.requests) == 1


async def test_native_completed_custom_string_call_survives_manual_checkpoint(responses):
    calls = []
    custom_finished = asyncio.Event()

    @function_tool
    def raw_editor(source: str) -> str:
        calls.append(source)
        custom_finished.set()
        return source.upper()

    @function_tool(needs_approval=True)
    def confirm(value: str) -> str:
        return value

    responses.respond(custom_call(), function_call("confirm", {"value": "pending"}, "manual"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use both tools.",
        tools=[raw_editor.as_custom(), confirm],
        provider=provider(responses),
    )
    async with timeout(20), agent.session(max_function_concurrency=2) as source:
        original = await source.submit("begin")
        assert (await next_request(source.interactions(), confirm)).call_id == "manual"
        await custom_finished.wait()
        saved = SessionState.from_json((await source.suspend()).to_json())

    async with timeout(20), agent.session(state=saved) as restored:
        turn = restored.restored_turn
        assert turn is not None and turn.turn_id == original.turn_id
        request = await next_request(restored.interactions(), confirm)
        assert request.call_id == "manual"
        await request.approve()
        assert (await turn.get_result()).final_output == "done"

    assert calls == [RAW_INPUT]
    output = tool_output(responses.requests[-1], "raw", "custom_tool_call_output")
    assert output == RAW_INPUT.upper()


async def test_native_pending_custom_string_call_remains_nonportable(responses):
    @function_tool(needs_approval=True)
    def raw_editor(source: str) -> str:
        pytest.fail("Pending custom callback must not run during suspension")

    tool = raw_editor.as_custom()
    responses.respond(custom_call())
    agent = Agent(
        instructions="Use raw_editor.",
        tools=[tool],
        provider=provider(responses),
    )
    async with timeout(20), agent.session() as session:
        await session.submit("begin")
        assert (await next_request(session.interactions(), tool)).call_id == "raw"
        with pytest.raises(RuntimeError, match="nonportable native work"):
            await session.suspend()


@pytest.mark.parametrize("payload", ["custom", "function", "unknown"])
async def test_native_agents_admits_granted_sdk_custom_callback(
    responses, tmp_path, monkeypatch, payload
):
    from agents import NativeAgents
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration

    calls = []

    @function_tool
    def raw_editor(source: str) -> str:
        calls.append(source)
        return source.upper()

    call = custom_call()
    if payload == "function":
        call = function_call("raw_editor", {"source": RAW_INPUT}, "raw")
    elif payload == "unknown":
        call["item"]["name"] = "unowned_editor"
    responses.respond(call)
    responses.respond(assistant_output("done"))
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Use raw_editor.",
            model="gpt-5.5",
            tools=[NativeAgents(), raw_editor.as_custom()],
            provider=provider(responses),
        ).session(environment=declaration(executor)) as session:
            assert (await (await session.submit("begin")).get_result()).final_output == "done"
    assert calls == ([RAW_INPUT] if payload == "custom" else [])
    output = tool_output(
        responses.requests[1],
        "raw",
        "function_call_output" if payload == "function" else "custom_tool_call_output",
    )
    if payload == "custom":
        assert output == RAW_INPUT.upper()
    elif payload == "function":
        assert "direct provider call payload" in output
    else:
        assert "unowned_editor" in output
