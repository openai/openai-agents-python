from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    ModelBehaviorError,
    Runner,
    ToolCallOutputItem,
    ToolNotFoundAction,
    ToolNotFoundErrorHandlerInput,
)

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


def _agent_with_one_tool() -> tuple[Agent[Any], FakeModel]:
    model = FakeModel()
    agent = Agent(
        name="test_agent",
        model=model,
        tools=[get_function_tool("real_tool", "tool_result")],
    )
    return agent, model


@pytest.mark.asyncio
async def test_no_handler_raises_model_behavior_error() -> None:
    """Backward compat: no handler → ``ModelBehaviorError`` bubbles up."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("search_linkedin", "{}")],
        ]
    )
    with pytest.raises(
        ModelBehaviorError, match="Tool search_linkedin not found in agent test_agent"
    ):
        await Runner.run(agent, input="hi")


@pytest.mark.asyncio
async def test_handler_returning_none_raises() -> None:
    """Handler can opt out by returning ``None``; the runner re-raises."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("search_linkedin", "{}")],
        ]
    )

    def handler(_: ToolNotFoundErrorHandlerInput[Any]) -> None:
        return None

    with pytest.raises(ModelBehaviorError, match="Tool search_linkedin not found"):
        await Runner.run(
            agent,
            input="hi",
            error_handlers={"tool_not_found": handler},
        )


@pytest.mark.asyncio
async def test_handler_returns_action_and_model_recovers() -> None:
    """Returning a ``ToolNotFoundAction`` injects a synthetic tool output and continues."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            # Turn 1: model hallucinates a tool
            [get_function_tool_call("search_linkedin", "{}")],
            # Turn 2: with the injected error, model "self-corrects" to a final answer
            [get_text_message("recovered")],
        ]
    )

    def handler(data: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        return ToolNotFoundAction(
            error_message=(
                f"Tool {data.tool_name!r} is not registered. "
                f"Available tools: {data.available_tools}"
            )
        )

    result = await Runner.run(
        agent,
        input="hi",
        error_handlers={"tool_not_found": handler},
    )

    assert result.final_output == "recovered"
    # The synthetic tool output was injected with the handler's message.
    outputs = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert len(outputs) == 1
    assert "search_linkedin" in str(outputs[0].output)
    assert "real_tool" in str(outputs[0].output)


@pytest.mark.asyncio
async def test_async_handler_is_awaited() -> None:
    """The handler may be a coroutine; the runner awaits it."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("bogus_tool", "{}")],
            [get_text_message("ok")],
        ]
    )

    called = {"count": 0}

    async def async_handler(
        data: ToolNotFoundErrorHandlerInput[Any],
    ) -> ToolNotFoundAction:
        called["count"] += 1
        return ToolNotFoundAction(error_message=f"no such tool: {data.tool_name}")

    result = await Runner.run(
        agent,
        input="hi",
        error_handlers={"tool_not_found": async_handler},
    )
    assert called["count"] == 1
    assert result.final_output == "ok"


@pytest.mark.asyncio
async def test_handler_input_contains_available_tools() -> None:
    """The handler input exposes ``available_tools`` — the list of names the agent has."""
    model = FakeModel()
    agent = Agent(
        name="multi_tool_agent",
        model=model,
        tools=[
            get_function_tool("alpha", "a"),
            get_function_tool("beta", "b"),
        ],
    )
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("gamma", "{}")],
            [get_text_message("done")],
        ]
    )

    seen_inputs: list[ToolNotFoundErrorHandlerInput[Any]] = []

    def handler(data: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        seen_inputs.append(data)
        return ToolNotFoundAction(error_message="nope")

    await Runner.run(agent, input="hi", error_handlers={"tool_not_found": handler})

    assert len(seen_inputs) == 1
    observed = seen_inputs[0]
    assert observed.tool_name == "gamma"
    assert set(observed.available_tools) == {"alpha", "beta"}
    assert observed.agent is agent
    # run_data is populated (defensive; the exact contents aren't part of the contract here).
    assert observed.run_data.last_agent is agent


@pytest.mark.asyncio
async def test_handler_invalid_return_raises_user_error() -> None:
    """Handlers must return ``ToolNotFoundAction | None``; other values fail loudly."""
    from agents.exceptions import UserError

    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("bogus", "{}")],
        ]
    )

    def bad_handler(_: ToolNotFoundErrorHandlerInput[Any]) -> str:
        return "not a ToolNotFoundAction"

    with pytest.raises(UserError, match="tool_not_found handler must return"):
        await Runner.run(
            agent,
            input="hi",
            error_handlers={"tool_not_found": bad_handler},  # type: ignore[typeddict-item]
        )


@pytest.mark.asyncio
async def test_streamed_runner_invokes_handler_and_recovers() -> None:
    """The streaming runner follows the same recovery path as ``Runner.run``."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("hallucinated", "{}")],
            [get_text_message("done-streamed")],
        ]
    )

    def handler(data: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        return ToolNotFoundAction(error_message=f"unknown tool {data.tool_name}")

    streamed = Runner.run_streamed(
        agent,
        input="hi",
        error_handlers={"tool_not_found": handler},
    )
    async for _ in streamed.stream_events():
        pass
    assert streamed.final_output == "done-streamed"


class _StructuredPayload(BaseModel):
    status: str


@pytest.mark.asyncio
async def test_litellm_json_tool_call_does_not_trigger_handler() -> None:
    """With ``output_type`` set, ``json_tool_call`` is a LiteLLM structured-output pseudo-call
    that :func:`process_model_response` handles by synthesizing a tool. The pre-scan must
    skip it so the user's ``tool_not_found`` handler is never invoked for a legitimate
    structured-output call, and the real lookup must not raise ``ModelBehaviorError``.
    """
    from agents import ModelResponse, Usage
    from agents.run_context import RunContextWrapper
    from agents.run_internal import run_loop
    from agents.run_internal.turn_preparation import get_output_schema
    from agents.run_internal.turn_resolution import (
        _resolve_tool_not_found_actions,
        collect_tool_not_found_calls,
    )

    agent = Agent(
        name="structured_agent",
        tools=[get_function_tool("real_tool", "tool_result")],
        output_type=_StructuredPayload,
    )
    response = ModelResponse(
        output=[
            get_function_tool_call(
                "json_tool_call",
                _StructuredPayload(status="ok").model_dump_json(),
                call_id="call_json_tool",
            )
        ],
        usage=Usage(),
        response_id="resp_json",
    )
    output_schema = get_output_schema(agent)

    # 1. Pre-scan must not flag `json_tool_call` as missing when an output schema is in use.
    missing = collect_tool_not_found_calls(
        all_tools=list(agent.tools),
        response=response,
        handoffs=[],
        output_schema=output_schema,
    )
    assert missing == []

    # 2. The resolver must return ``None`` — no handler invocation — even with a handler
    #    registered, because the pre-scan found nothing.
    handler_calls: list[str] = []

    def handler(data: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        handler_calls.append(data.tool_name)
        return ToolNotFoundAction(error_message="should not be called")

    resolved = await _resolve_tool_not_found_actions(
        error_handlers={"tool_not_found": handler},
        agent=agent,
        all_tools=list(agent.tools),
        handoffs=[],
        response=response,
        output_schema=output_schema,
        original_input="hi",
        pre_step_items=[],
        raw_responses_so_far=[],
        context_wrapper=RunContextWrapper(None),
    )
    assert resolved is None
    assert handler_calls == []

    # 3. The real lookup must not raise — it synthesizes the json_tool_call tool.
    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=list(agent.tools),
        response=response,
        output_schema=output_schema,
        handoffs=[],
    )
    assert len(processed.functions) == 1
    assert processed.functions[0].tool_call.name == "json_tool_call"


@pytest.mark.asyncio
async def test_handler_exception_propagates() -> None:
    """A handler that raises should surface the exception — the SDK must not swallow it.

    This pins the contract: buggy handler code is the caller's bug, not the SDK's.
    """
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("bogus", "{}")],
        ]
    )

    class HandlerBoom(RuntimeError):
        pass

    def handler(_: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        raise HandlerBoom("handler exploded")

    with pytest.raises(HandlerBoom, match="handler exploded"):
        await Runner.run(
            agent,
            input="hi",
            error_handlers={"tool_not_found": handler},
        )


@pytest.mark.asyncio
async def test_multiple_unknown_calls_in_one_batch_all_recover() -> None:
    """When the model emits multiple unknown tool calls in a single turn, the handler is
    invoked once per call and one synthetic output is produced for each."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("ghost_a", "{}", call_id="call_1"),
                get_function_tool_call("ghost_b", "{}", call_id="call_2"),
                get_function_tool_call("ghost_c", "{}", call_id="call_3"),
            ],
            [get_text_message("recovered-3")],
        ]
    )

    seen_names: list[str] = []

    def handler(data: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        seen_names.append(data.tool_name)
        return ToolNotFoundAction(error_message=f"unknown tool: {data.tool_name}")

    result = await Runner.run(
        agent,
        input="hi",
        error_handlers={"tool_not_found": handler},
    )

    assert seen_names == ["ghost_a", "ghost_b", "ghost_c"]
    outputs = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert len(outputs) == 3
    output_strs = [str(item.output) for item in outputs]
    assert any("ghost_a" in s for s in output_strs)
    assert any("ghost_b" in s for s in output_strs)
    assert any("ghost_c" in s for s in output_strs)
    assert result.final_output == "recovered-3"


@pytest.mark.asyncio
async def test_synthetic_output_round_trips_through_to_input_list() -> None:
    """``result.to_input_list()`` must include the synthesized function_call_output so that
    the next turn's model input is well-formed."""
    agent, model = _agent_with_one_tool()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("phantom", "{}", call_id="call_phantom")],
            [get_text_message("ok")],
        ]
    )

    recovery_msg = "phantom is not a real tool; use real_tool."

    def handler(_: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
        return ToolNotFoundAction(error_message=recovery_msg)

    result = await Runner.run(
        agent,
        input="hi",
        error_handlers={"tool_not_found": handler},
    )

    input_list = result.to_input_list()
    synthesized = [
        item
        for item in input_list
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert len(synthesized) == 1
    assert synthesized[0].get("call_id") == "call_phantom"
    assert synthesized[0].get("output") == recovery_msg
