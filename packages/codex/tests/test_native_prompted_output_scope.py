"""The real model request must not give old SDK output instructions global scope."""

from __future__ import annotations

import json

import pytest

from agents import Agent, FunctionTool, NativeOutput, PromptedOutput, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_output_modes import Report, output_format
from tests.test_review_ii_steering_tail import ProjectContinuation


def assert_prompted_scope(request: dict, root: str) -> str:
    # Inspect the actual user message emitted by Core, not SDK input helpers.
    # Completed history must remain present; editing it would lose the request.
    messages = [
        json.dumps(item)
        for item in request["input"]
        if item.get("role") == "user" and root in json.dumps(item)
    ]
    assert len(messages) == 1
    message = messages[0]
    assert "Return only JSON" in message
    assert "only for this request" in message
    assert "including its automatic continuations and tool results" in message
    assert "does not apply to later requests" in message
    return message


@pytest.mark.parametrize(
    "later_mode, later_reply, later_value",
    [
        pytest.param(str, "ordinary text", "ordinary text", id="plain"),
        pytest.param(PromptedOutput(list[int]), "[3,5]", [3, 5], id="different-prompted"),
        pytest.param(NativeOutput(int), '{"response":7}', 7, id="native"),
    ],
)
async def test_prior_prompted_guidance_is_scoped_on_each_later_request(
    responses: ResponsesFixture, later_mode, later_reply, later_value
):
    # Fixed peer responses advance the real native thread. They deliberately do
    # not infer or echo the mode: only captured requests prove instruction scope.
    responses.respond(assistant_output('{"label":"first-result"}'))
    responses.respond(assistant_output(later_reply))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        old = await session.submit("SYNTHETIC_ROOT_FIRST", output_type=PromptedOutput(Report))
        assert (await old.get_result()).final_output == Report(label="first-result")
        later = await session.submit("SYNTHETIC_ROOT_LATER", output_type=later_mode)
        assert (await later.get_result()).final_output == later_value
        assert (await old.get_result()).final_output == Report(label="first-result")
    first_request, next_request_ = responses.requests
    assert output_format(first_request) is None
    original = assert_prompted_scope(first_request, "SYNTHETIC_ROOT_FIRST")
    assert assert_prompted_scope(next_request_, "SYNTHETIC_ROOT_FIRST") == original
    if isinstance(later_mode, PromptedOutput):
        assert output_format(next_request_) is None
        current = assert_prompted_scope(next_request_, "SYNTHETIC_ROOT_LATER")
        assert "array" in current and "integer" in current and "label" not in current
    elif isinstance(later_mode, NativeOutput):
        schema = output_format(next_request_)["schema"]
        assert schema["properties"]["response"]["type"] == "integer"
    else:
        assert output_format(next_request_) is None


async def test_prompted_scope_covers_live_tool_then_idle_restore_and_plain_root(responses):
    @function_tool
    def lookup(key: str) -> str:
        return f"record-{key}"

    agent = Agent(instructions="Look up the record.", tools=[lookup], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "scope-live-A"))
    responses.respond(assistant_output('{"label":"record-A"}'))
    responses.respond(assistant_output("ordinary answer after restore"))
    async with timeout(25):
        async with agent.session() as session:
            first = await session.submit("SYNTHETIC_ROOT_TOOL", output_type=PromptedOutput(Report))
            assert (await first.get_result()).final_output.label == "record-A"
            state = SessionState.from_json((await session.save_state()).to_json())
            assert state.format_version == 4
        async with agent.session(state=state) as restored:
            assert restored.restored_turn is None
            plain = await restored.submit("SYNTHETIC_ROOT_AFTER_IDLE")
            assert (await plain.get_result()).final_output == "ordinary answer after restore"
    start, tool_resume, after_idle = responses.requests
    original = assert_prompted_scope(start, "SYNTHETIC_ROOT_TOOL")
    assert assert_prompted_scope(tool_resume, "SYNTHETIC_ROOT_TOOL") == original
    assert assert_prompted_scope(after_idle, "SYNTHETIC_ROOT_TOOL") == original
    assert tool_output(tool_resume, "scope-live-A") == "record-A"
    assert all(output_format(request) is None for request in responses.requests)


async def test_manual_prompted_restore_keeps_scope_until_new_native_root(responses):
    tool = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Read.", tools=[tool], provider=provider(responses))
    output = PromptedOutput(Report)
    responses.respond(function_call("lookup", {"key": "A"}, "scope-manual-A"))
    async with timeout(25):
        async with agent.session() as session:
            old = await session.submit("SYNTHETIC_ROOT_MANUAL", output_type=output)
            await next_request(session.interactions(), tool)
            state = SessionState.from_json((await session.suspend()).to_json())
            assert state.format_version == 5 and state.native_continuation is not None
            assert state.native_continuation.output_schema is None
        responses.respond(assistant_output('{"label":"manual-A"}'))
        responses.respond(assistant_output('{"response":21}'))
        async with agent.with_output_type(output).session(state=state) as restored:
            turn = restored.restored_turn
            assert turn is not None and turn.turn_id == old.turn_id
            fresh = await next_request(restored.interactions(), tool)
            await fresh.respond("manual-A")
            assert (await turn.get_result()).final_output.label == "manual-A"
            native = await restored.submit(
                "SYNTHETIC_ROOT_AFTER_MANUAL", output_type=NativeOutput(int)
            )
            assert (await native.get_result()).final_output == 21
    start, manual_resume, after_manual = responses.requests
    original = assert_prompted_scope(start, "SYNTHETIC_ROOT_MANUAL")
    assert assert_prompted_scope(manual_resume, "SYNTHETIC_ROOT_MANUAL") == original
    assert assert_prompted_scope(after_manual, "SYNTHETIC_ROOT_MANUAL") == original
    assert tool_output(manual_resume, "scope-manual-A") == "manual-A"
    assert output_format(start) is None and output_format(manual_resume) is None
    assert output_format(after_manual)["schema"]["properties"]["response"]["type"] == "integer"


async def test_prompted_scope_remains_for_sdk_automatic_successor_then_expires(
    responses, monkeypatch
):
    # Only tail admission is projected because the peer cannot deterministically
    # hit Core's gap. The actual C3 engine supplies and captures every request.
    responses.respond(assistant_output('{"label":"original"}'))
    responses.respond(assistant_output('{"label":"successor"}'))
    responses.respond(assistant_output("later default"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
        old = await session.submit("SYNTHETIC_ROOT_AUTO", output_type=PromptedOutput(Report))
        completed = await old.get_result()
        assert completed.final_output.label == "successor"
        assert completed.turn is old
        assert await old.get_result() is completed
        plain = await session.submit("SYNTHETIC_ROOT_AFTER_AUTO")
        assert (await plain.get_result()).final_output == "later default"
    start, auto, next_root = responses.requests
    original = assert_prompted_scope(start, "SYNTHETIC_ROOT_AUTO")
    assert assert_prompted_scope(auto, "SYNTHETIC_ROOT_AUTO") == original
    assert assert_prompted_scope(next_root, "SYNTHETIC_ROOT_AUTO") == original
    assert all(output_format(request) is None for request in responses.requests)
