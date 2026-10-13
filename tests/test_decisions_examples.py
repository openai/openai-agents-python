"""Exercise Decisions recipes without network calls or live classification."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI

from agents import InputGuardrailTripwireTriggered, RunConfig, Runner
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel
from examples.decisions import classify, model_router, tool_risk_gate
from tests.test_responses import get_function_tool_call, get_text_message

CONFIG = RunConfig(tracing_disabled=True)


def client_with_answers(*answers: SimpleNamespace) -> tuple[AsyncOpenAI, AsyncMock]:
    create = AsyncMock(return_value=SimpleNamespace(answers=list(answers)))
    return cast(AsyncOpenAI, SimpleNamespace(decisions=SimpleNamespace(create=create))), create


@pytest.mark.parametrize("probability", [0.2, 0.95, None])
async def test_input_classification_blocks_before_agent(probability: float | None) -> None:
    answer = SimpleNamespace(
        name="in_scope",
        type="refusal" if probability is None else "predicate",
        probability=probability,
    )
    client, create = client_with_answers(answer)
    model = ScriptedModel([[get_text_message("help")]])
    agent = classify.create_agent(client).clone(model=model)
    if probability is not None and probability >= 0.8:
        result = await Runner.run(agent, "Dashboard help", run_config=CONFIG)
        assert result.final_output == "help"
        assert len(model.calls) == 1
    else:
        with pytest.raises(InputGuardrailTripwireTriggered):
            await Runner.run(agent, "Dashboard help", run_config=CONFIG)
        assert not model.calls
    assert agent.input_guardrails[0].run_in_parallel is False
    assert create.await_count == 1


@pytest.mark.parametrize("choice", ["fast", "powerful", None])
async def test_route_and_refusal_fallback(choice: str | None) -> None:
    client, _ = client_with_answers(
        SimpleNamespace(name="route", type="refusal" if choice is None else "choice", choice=choice)
    )
    assert (
        await model_router.choose_model(client, "Explain Python lists")
        == (model_router.MODELS[choice or "powerful"])
    )


@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("probability", [0.1, 0.9, None])
async def test_tool_policy_and_approval_resume(approval: bool, probability: float | None) -> None:
    client, create = client_with_answers(
        SimpleNamespace(
            name="needs_review",
            type="refusal" if probability is None else "predicate",
            probability=probability,
        )
    )
    agent = tool_risk_gate.create_agent(client, require_approval=approval).clone(
        model=ScriptedModel(
            [
                [get_function_tool_call("publish_announcement", '{"text":"Release Friday"}')],
                [get_text_message("Not published")],
            ]
        )
    )
    result = await Runner.run(agent, "Publish the announcement", run_config=CONFIG)
    needs_review = probability is None or probability >= 0.5
    if needs_review and approval:
        assert len(result.interruptions) == 1
        assert not any(isinstance(item, ToolCallOutputItem) for item in result.new_items)
        state = result.to_state()
        state.approve(result.interruptions[0])
        result = await Runner.run(agent, state, run_config=CONFIG)
        assert create.await_count == 1
    if not needs_review or approval:
        assert result.final_output == "Simulated publication: Release Friday"
    else:
        outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
        assert outputs == ["The announcement needs editorial review and was not published."]


async def test_router_missing_answer_and_api_failure_are_distinct() -> None:
    client, create = client_with_answers()
    assert await model_router.choose_model(client, "hello") == model_router.MODELS["powerful"]
    create.side_effect = RuntimeError("synthetic service failure")
    with pytest.raises(RuntimeError, match="synthetic service failure"):
        await model_router.choose_model(client, "hello")


@pytest.mark.parametrize("approval", [False, True])
async def test_tool_classifier_failure_never_invokes_tool(approval: bool) -> None:
    client, create = client_with_answers()
    create.side_effect = RuntimeError("synthetic service failure")
    agent = tool_risk_gate.create_agent(client, require_approval=approval).clone(
        model=ScriptedModel(
            [[get_function_tool_call("publish_announcement", '{"text":"Release Friday"}')]]
        )
    )
    # Observe the actual invocation boundary, not just the final error message.
    from agents import FunctionTool

    function = cast(FunctionTool, agent.tools[0])
    invoke = AsyncMock(wraps=function.on_invoke_tool)
    function.on_invoke_tool = invoke
    with pytest.raises(Exception, match="synthetic service failure"):
        await Runner.run(agent, "Publish the announcement", run_config=CONFIG)
    invoke.assert_not_awaited()
