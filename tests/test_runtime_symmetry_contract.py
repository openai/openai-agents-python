from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents import Agent, Runner, Tool, Usage
from agents.items import ToolApprovalItem
from agents.result import RunResult, RunResultStreaming
from agents.usage import serialize_usage

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message
from .testing_processor import SPAN_PROCESSOR_TESTING, fetch_normalized_spans
from .utils.simple_session import SimpleListSession


def _item_projection(item: Any) -> dict[str, Any]:
    if isinstance(item, ToolApprovalItem):
        return {
            "type": type(item).__name__,
            "name": item.name,
            "call_id": item.call_id,
        }
    payload = item.to_input_item()
    return {
        key: payload.get(key)
        for key in ("type", "name", "call_id", "output")
        if payload.get(key) is not None
    }


def _result_projection(result: RunResult | RunResultStreaming) -> dict[str, Any]:
    return {
        "final_output": result.final_output,
        "last_agent": result.last_agent.name,
        "new_items": [_item_projection(item) for item in result.new_items],
        "interruptions": [
            {
                "name": item.name,
                "call_id": item.call_id,
            }
            for item in result.interruptions
        ],
        "usage": serialize_usage(result.context_wrapper.usage),
    }


def _detailed_usage() -> Usage:
    return Usage(
        requests=1,
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        input_tokens_details=InputTokensDetails.model_validate(
            {"cached_tokens": 3, "cache_write_tokens": 2}
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=4),
    )


def _assert_detailed_usage(usage: dict[str, Any]) -> None:
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["total_tokens"] > 0
    assert usage["input_tokens_details"][0]["cached_tokens"] > 0
    assert usage["input_tokens_details"][0]["cache_write_tokens"] > 0
    assert usage["output_tokens_details"][0]["reasoning_tokens"] > 0
    assert usage["request_usage_entries"]


def _trace_projection() -> list[dict[str, Any]]:
    def project_node(node: dict[str, Any]) -> dict[str, Any]:
        projected = {key: node[key] for key in ("workflow_name", "type") if key in node}
        projected["has_error"] = node.get("error") is not None
        children = node.get("children")
        if isinstance(children, list):
            projected["children"] = [project_node(child) for child in children]
        return projected

    return [project_node(trace) for trace in fetch_normalized_spans()]


async def _run(
    agent: Agent[Any],
    *,
    streamed: bool,
    session: SimpleListSession | None = None,
) -> RunResult | RunResultStreaming:
    if not streamed:
        return await Runner.run(agent, "run the contract", session=session)
    result = Runner.run_streamed(agent, "run the contract", session=session)
    async for _event in result.stream_events():
        pass
    return result


@pytest.mark.parametrize("streamed", [False, True])
async def test_fake_model_records_every_model_visible_request_field(streamed: bool) -> None:
    model = FakeModel()
    model.set_next_output([get_text_message("READY")])
    await _run(Agent(name="request-contract-agent", model=model), streamed=streamed)

    assert set(model.last_turn_args) == {
        "system_instructions",
        "input",
        "model_settings",
        "tools",
        "output_schema",
        "handoffs",
        "tracing",
        "previous_response_id",
        "conversation_id",
        "prompt",
    }


@pytest.mark.parametrize("scenario", ["basic", "function-tool"])
async def test_streamed_and_nonstreamed_runs_have_matching_semantics(scenario: str) -> None:
    projections: list[dict[str, Any]] = []
    for streamed in (False, True):
        SPAN_PROCESSOR_TESTING.clear()
        model = FakeModel(tracing_enabled=True)
        model.set_hardcoded_usage(_detailed_usage())
        tools: list[Tool] = []
        if scenario == "function-tool":
            model.add_multiple_turn_outputs(
                [
                    [get_function_tool_call("release_check", "{}", call_id="call-release")],
                    [get_text_message("READY")],
                ]
            )
            tools = [get_function_tool("release_check", "checked")]
        else:
            model.set_next_output([get_text_message("READY")])
        agent = Agent(name="symmetry-agent", model=model, tools=tools)
        session = SimpleListSession(session_id=f"{scenario}-{streamed}")
        result = await _run(agent, streamed=streamed, session=session)
        projections.append(
            {
                "result": _result_projection(result),
                "session_items": await session.get_items(),
                "traces": _trace_projection(),
            }
        )

    assert projections[0] == projections[1]
    for projection in projections:
        _assert_detailed_usage(projection["result"]["usage"])
        assert projection["session_items"]
        assert projection["traces"]


async def test_streamed_and_nonstreamed_runs_raise_the_same_exception_class() -> None:
    exception_classes: list[type[BaseException]] = []
    for streamed in (False, True):
        model = FakeModel()
        model.set_next_output(RuntimeError("release contract failure"))
        agent = Agent(name="symmetry-agent", model=model)

        with pytest.raises(RuntimeError) as exc_info:
            await _run(agent, streamed=streamed)
        exception_classes.append(type(exc_info.value))

    assert exception_classes == [RuntimeError, RuntimeError]


async def test_approval_resume_cross_modes_have_matching_semantics() -> None:
    projections: list[dict[str, Any]] = []
    for start_streamed, resume_streamed in ((True, False), (False, True)):
        SPAN_PROCESSOR_TESTING.clear()
        model = FakeModel(tracing_enabled=True)
        model.set_hardcoded_usage(_detailed_usage())
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("release_check", "{}", call_id="call-release")],
                [get_text_message("READY")],
            ]
        )
        tool = get_function_tool("release_check", "checked")
        tool.needs_approval = True
        agent = Agent(name="symmetry-agent", model=model, tools=[tool])
        session = SimpleListSession(session_id=f"approval-{start_streamed}-{resume_streamed}")

        first = await _run(agent, streamed=start_streamed, session=session)
        assert len(first.interruptions) == 1
        state = first.to_state()
        state.approve(first.interruptions[0])

        resumed: RunResult | RunResultStreaming
        if resume_streamed:
            streaming_result = Runner.run_streamed(agent, state, session=session)
            async for _event in streaming_result.stream_events():
                pass
            resumed = streaming_result
        else:
            resumed = await Runner.run(agent, state, session=session)

        projections.append(
            {
                "first": _result_projection(first),
                "resumed": _result_projection(resumed),
                "session_items": await session.get_items(),
                "traces": _trace_projection(),
            }
        )

    assert projections[0] == projections[1]
    for projection in projections:
        _assert_detailed_usage(projection["first"]["usage"])
        _assert_detailed_usage(projection["resumed"]["usage"])
        assert projection["session_items"]
        assert projection["traces"]
