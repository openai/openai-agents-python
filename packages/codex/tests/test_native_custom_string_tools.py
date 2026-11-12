"""Exercise custom string declarations through the native Responses route."""

import json
from pathlib import Path

import pytest

from agents import Agent, function_tool
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
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "raw_editor",
                "call_id": "raw",
                "input": RAW_INPUT,
            },
        }
    )
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
