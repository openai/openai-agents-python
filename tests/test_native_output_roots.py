"""Exercise non-object outputs through native requests and typed completion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from typing import Any

import pytest
from pydantic import BaseModel, RootModel, ValidationInfo, model_validator

from agents import Agent, NativeProvider, OutputValidationError, function_tool
from agents._events import SessionItem
from agents.observation import OutputTextDelta, TurnFinished
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent_observation import text_response
from tests.test_native_provider_websockets import peer


class IntegerRoot(RootModel[int]):
    pass


class TextRoot(RootModel[str]):
    pass


class ChoiceRoot(RootModel[int | str | None]):
    pass


class ObjectValue(BaseModel):
    response: str


class ObjectRoot(RootModel[ObjectValue]):
    pass


class Node(BaseModel):
    name: str
    children: list[Node]


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-output-test")


def assistant_text(page: list[SessionItem]) -> list[str]:
    return [
        content["text"]
        for item in page
        if item["type"] == "message" and item["role"] == "assistant"
        for content in item["content"]
        if content["type"] == "output_text"
    ]


@pytest.mark.parametrize(
    "output_type,value,expected",
    [
        (int, 7, 7),
        (list[int], [1, 2], [1, 2]),
        (int, 0, 0),
        (list[int], [], []),
        (bool, False, False),
        (type(None), None, None),
        (IntegerRoot, 0, IntegerRoot(0)),
        (TextRoot, "", TextRoot("")),
        (ChoiceRoot, None, ChoiceRoot(None)),
        (ChoiceRoot, "choice", ChoiceRoot("choice")),
    ],
)
async def test_supported_non_object_outputs(
    responses: ResponsesFixture, output_type: Any, value: Any, expected: Any
) -> None:
    raw = json.dumps({"response": value})
    responses.respond(*text_response("typed-output", raw))
    agent = Agent(
        instructions="Return the requested value.",
        provider=provider(responses),
        output_type=output_type,
    )
    async with timeout(10), agent.session() as session:
        handle = await session.submit("start")
        completed = await (handle).get_result()
        assert completed.final_output == expected
        assert type(completed.final_output) is type(expected)
        assert completed.turn is handle
        assert assistant_text([entry async for entry in session.items()]) == [raw]
        observed = [event async for event in handle.events()]
        assert (
            "".join(event.delta for event in observed if isinstance(event, OutputTextDelta)) == raw
        )
    [request] = responses.requests
    output_format = request["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    schema = output_format["schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["response"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"response"}


@pytest.mark.parametrize("output_type", [ObjectValue, ObjectRoot])
async def test_existing_object_and_object_ref_roots_stay_unwrapped(
    responses: ResponsesFixture, output_type: Any
) -> None:
    raw = '{"response":"an ordinary object field"}'
    responses.respond(assistant_output(raw))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=output_type)
    async with timeout(10), agent.session() as session:
        result = await (await session.submit("object")).get_result()
    expected = ObjectValue(response="an ordinary object field")
    assert result.final_output == (ObjectRoot(expected) if output_type is ObjectRoot else expected)
    schema = responses.requests[0]["text"]["format"]["schema"]
    assert schema["properties"]["response"]["type"] == "string"
    assert set(schema["properties"]) == {"response"}


@pytest.mark.parametrize("output_type", [str, None])
async def test_plaintext_outputs_stay_unwrapped(
    responses: ResponsesFixture, output_type: Any
) -> None:
    responses.respond(assistant_output("plain text, not JSON"))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=output_type)
    async with timeout(10):
        result = await agent.run("text")
    assert result.final_output == "plain text, not JSON"
    assert responses.requests[0].get("text", {}).get("format") is None


async def test_recursive_list_definitions_resolve_from_the_wire_root(
    responses: ResponsesFixture,
) -> None:
    values = [{"name": "parent", "children": [{"name": "leaf", "children": []}]}]
    responses.respond(assistant_output(json.dumps({"response": values})))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=list[Node])
    async with timeout(10), agent.session() as session:
        result = await (await session.submit("tree")).get_result()
    assert result.final_output == [Node(name="parent", children=[Node(name="leaf", children=[])])]
    schema = responses.requests[0]["text"]["format"]["schema"]
    assert schema["properties"]["response"]["items"]["$ref"] == "#/$defs/Node"

    def check_references(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/")
                target = schema
                for segment in value["$ref"][2:].split("/"):
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
                assert isinstance(target, dict)
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(schema)
    assert schema["$defs"]["Node"]["properties"]["children"]["items"]["$ref"] == "#/$defs/Node"


async def test_json_sensitive_validation_runs_once(responses: ResponsesFixture) -> None:
    calls: list[tuple[str, object]] = []

    class JsonDate(RootModel[date]):
        @model_validator(mode="before")
        @classmethod
        def require_json(cls, value: Any, info: ValidationInfo) -> Any:
            calls.append((info.mode, value))
            if info.mode != "json":
                raise ValueError("This output must be validated directly from JSON")
            return value

    responses.respond(assistant_output('{"response":"2040-12-24"}'))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=JsonDate)
    async with timeout(10), agent.session() as session:
        result = await (await session.submit("date")).get_result()
    assert isinstance(result.final_output, JsonDate)
    assert result.final_output.root == date(2040, 12, 24)
    assert calls == [("json", "2040-12-24")]


@pytest.mark.parametrize(
    "output_type,bad,good,expected",
    [
        (int, "{}", '{"response":8}', 8),
        (int, '{"response":"wrong"}', '{"response":8}', 8),
        (int, '{"response":', '{"response":8}', 8),
        (int, "7", '{"response":8}', 8),
        (int, '{"response":7,"extra":true}', '{"response":8}', 8),
        (type(None), "{}", '{"response":null}', None),
    ],
)
async def test_invalid_envelope_fails_wait_preserves_history_and_allows_recovery(
    responses: ResponsesFixture, output_type: Any, bad: str, good: str, expected: Any
) -> None:
    responses.respond(assistant_output(bad))
    responses.respond(assistant_output(good))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=output_type)
    async with timeout(10), agent.session() as session:
        first = await session.submit("invalid")
        with pytest.raises(OutputValidationError) as failed:
            await (first).get_result()
        assert failed.value.raw_output == bad
        events = [event async for event in first.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"
        second = await session.submit("valid")
        assert (await (second).get_result()).final_output == expected
        assert assistant_text([entry async for entry in session.items()]) == [bad, good]
        assert second.turn_id != first.turn_id
    assert len(responses.requests) == 2


def test_unsupported_shapes_fail_without_loading_native() -> None:
    # A fresh interpreter makes absence of native work observable. This guard
    # never supplies a replacement engine or changes schema validation.
    script = """
import asyncio
import importlib.abc
import sys
from typing import Any
from agents import Agent, NativeProvider

class NoNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'agents._native':
            raise AssertionError('unsupported output reached native loading')
        return None

assert 'agents._native' not in sys.modules
sys.meta_path.insert(0, NoNative())
async def check():
    for output_type in (Any, dict[str, int], set[int], tuple[int, str]):
        agent = Agent(instructions='Answer.', output_type=output_type,
            provider=NativeProvider(base_url='http://127.0.0.1:1/v1', api_key='synthetic'))
        try:
            async with agent.session():
                raise AssertionError('unsupported output opened a Session')
        except ValueError as error:
            assert 'output_type' in str(error)
    assert 'agents._native' not in sys.modules
asyncio.run(check())
"""
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_callback_then_typed_final_through_runner(responses: ResponsesFixture) -> None:
    calls: list[int] = []

    @function_tool
    async def double(value: int) -> int:
        calls.append(value)
        return value * 2

    responses.respond(function_call("double", {"value": 4}, "double-call"))
    responses.respond(assistant_output('{"response":8}'))
    agent = Agent(
        instructions="Use double and answer.",
        provider=provider(responses),
        tools=[double],
        output_type=int,
    )
    async with timeout(10):
        result = await agent.run("double four")
    assert result.final_output == 8
    assert calls == [4]
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "double-call") == "8"


async def test_restore_recreates_output_conversion_and_retains_raw_history(
    responses: ResponsesFixture,
) -> None:
    first_text, next_text = '{"response":[]}', '{"response":[2,3]}'
    responses.respond(assistant_output(first_text))
    responses.respond(assistant_output(next_text))
    agent = Agent(
        instructions="Return numbers.", provider=provider(responses), output_type=list[int]
    )
    async with timeout(15):
        async with agent.session() as first:
            assert (await (await first.submit("first")).get_result()).final_output == []
            state = SessionState.from_json((await first.save_state()).to_json())
        async with agent.session(state=state) as restored:
            assert assistant_text([entry async for entry in restored.items()]) == [first_text]
            result = await (await restored.submit("continue")).get_result()
            assert result.final_output == [2, 3]
            assert assistant_text([entry async for entry in restored.items()]) == [
                first_text,
                next_text,
            ]
    assert len(responses.requests) == 2
    assert responses.requests[0]["text"]["format"] == responses.requests[1]["text"]["format"]


async def test_websocket_typed_output_uses_the_same_required_envelope() -> None:
    async with timeout(15), peer() as websocket:
        websocket.respond(assistant_output('{"response":0}'))
        agent = Agent(instructions="Answer.", provider=websocket.provider(), output_type=int)
        async with agent.session() as session:
            result = await (await session.submit("zero")).get_result()
        assert result.final_output == 0
        assert len(websocket.requests) == 1
        assert not websocket.http_requests
        [request] = websocket.requests
        schema = request["text"]["format"]["schema"]
        assert schema["required"] == ["response"]
        assert schema["properties"]["response"]["type"] == "integer"
