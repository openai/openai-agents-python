from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, Runner, Tool, Usage
from agents.result import RunResult, RunResultStreaming

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


def _item_projection(item: Any) -> dict[str, Any]:
    payload = item.to_input_item()
    return {
        key: payload.get(key)
        for key in ("type", "role", "name", "call_id", "output")
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
                "call_id": item.to_input_item().get("call_id"),
            }
            for item in result.interruptions
        ],
        "usage_requests": result.context_wrapper.usage.requests,
    }


async def _run(agent: Agent[Any], *, streamed: bool) -> RunResult | RunResultStreaming:
    if not streamed:
        return await Runner.run(agent, "run the contract")
    result = Runner.run_streamed(agent, "run the contract")
    async for _event in result.stream_events():
        pass
    return result


@pytest.mark.parametrize("scenario", ["basic", "function-tool"])
async def test_streamed_and_nonstreamed_runs_have_matching_semantics(scenario: str) -> None:
    projections: list[dict[str, Any]] = []
    for streamed in (False, True):
        model = FakeModel()
        model.set_hardcoded_usage(Usage(requests=1))
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
        projections.append(_result_projection(await _run(agent, streamed=streamed)))

    assert projections[0] == projections[1]


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
