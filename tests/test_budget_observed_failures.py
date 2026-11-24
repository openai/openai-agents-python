"""Failed-attempt usage is opt-in while successful observations retain upstream semantics."""

import pytest
from openai.types.responses import ResponseFailedEvent

from agents import (
    Agent,
    ModelBehaviorError,
    ModelRetrySettings,
    ModelSettings,
    RunBudget,
    RunBudgetError,
    RunConfig,
    Runner,
    Usage,
)
from agents.decorators import tool
from agents.models._response_terminal import response_terminal_failure_error
from agents.retry import RetryDecision
from agents.testing import ModelStep, ScriptedModel
from tests.model_test_helpers import get_response_obj
from tests.test_responses import get_function_tool_call, get_text_message


async def execute(agent, budget, streamed):
    config = RunConfig(
        budget=budget, tracing_disabled=True, tool_not_found_behavior="return_error_to_model"
    )
    if not streamed:
        return await Runner.run(agent, "go", run_config=config)
    result = Runner.run_streamed(agent, "go", run_config=config)
    async for _ in result.stream_events():
        pass
    return result


def failure(tokens):
    response = get_response_obj([], usage=Usage(requests=1, total_tokens=tokens))
    return response_terminal_failure_error("response.failed", response)


def retry_settings():
    return ModelSettings(
        retry=ModelRetrySettings(
            max_retries=1,
            policy=lambda _: RetryDecision(retry=True, delay=0, approve_unsafe_replay=True),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("include_failed", [False, True], ids=["upstream-default", "opt-in"])
async def test_failed_100_then_retry_1_applies_selected_usage_policy(streamed, include_failed):
    # The adapter's terminal-error producer supplies actual failed-response usage.
    model = ScriptedModel(
        [
            failure(100),
            ModelStep(
                output=[get_function_tool_call("lookup")], usage=Usage(requests=1, total_tokens=1)
            ),
            ModelStep(output=[get_text_message("done")], usage=Usage(requests=1, total_tokens=1)),
        ]
    )
    effects = []

    @tool
    async def lookup() -> str:
        effects.append("lookup")
        return "found"

    agent = Agent(name="root", model=model, tools=[lookup], model_settings=retry_settings())
    budget = RunBudget(max_tokens=50, include_failed_attempt_usage=include_failed)
    if include_failed:
        with pytest.raises(RunBudgetError) as caught:
            await execute(agent, budget, streamed)
        assert caught.value.limit == "tokens"
        data = caught.value.run_data
        assert data is not None and data.context_wrapper._budget is not None
        assert data.context_wrapper.usage.total_tokens == 101
        assert data.context_wrapper._budget.tokens == 101
        assert data.context_wrapper._budget.requests == 1
        assert len(model.calls) == 2
    else:
        result = await execute(agent, budget, streamed)
        assert result.final_output == "done"
        assert result.context_wrapper._budget is not None
        assert result.context_wrapper._budget.tokens == 2
        assert result.context_wrapper._budget.requests == 2
        assert result.context_wrapper.usage.total_tokens == 102
        assert len(model.calls) == 3
    assert effects == ["lookup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_known_failure_is_observed_once_before_retry_aggregation(streamed):
    model = ScriptedModel(
        [
            failure(5),
            ModelStep(
                output=[get_function_tool_call("unknown")], usage=Usage(requests=1, total_tokens=7)
            ),
            ModelStep(output=[get_text_message("done")], usage=Usage(requests=1, total_tokens=11)),
        ]
    )
    result = await execute(
        Agent(name="root", model=model, model_settings=retry_settings()),
        RunBudget(max_outer_requests=2, max_tokens=30, include_failed_attempt_usage=True),
        streamed,
    )
    assert result.final_output == "done"
    assert result.context_wrapper._budget is not None
    assert result.context_wrapper._budget.requests == 2
    assert result.context_wrapper._budget.tokens == 23
    assert result.context_wrapper.usage.total_tokens == 23


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("include_failed", [False, True])
async def test_terminal_failure_keeps_public_usage_and_opt_in_budget_usage(
    streamed, include_failed
):
    model = ScriptedModel([failure(9)])
    with pytest.raises(ModelBehaviorError) as caught:
        await execute(
            Agent(name="root", model=model),
            RunBudget(max_tokens=50, include_failed_attempt_usage=include_failed),
            streamed,
        )
    data = caught.value.run_data
    assert data is not None and data.context_wrapper._budget is not None
    assert data.context_wrapper._budget.tokens == (9 if include_failed else 0)
    assert data.context_wrapper._budget.requests == 1
    assert data.context_wrapper.usage.total_tokens == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("missing", ["stop", "continue"])
async def test_failure_without_usage_does_not_invent_a_missing_observation(streamed, missing):
    model = ScriptedModel(
        [
            ModelBehaviorError("unreported failure"),
            ModelStep(
                output=[get_function_tool_call("unknown")], usage=Usage(requests=1, total_tokens=1)
            ),
            ModelStep(output=[get_text_message("done")], usage=Usage(requests=1, total_tokens=1)),
        ]
    )
    result = await execute(
        Agent(name="root", model=model, model_settings=retry_settings()),
        RunBudget(
            max_tokens=3,
            max_outer_requests=2,
            missing_usage=missing,
            include_failed_attempt_usage=True,
        ),
        streamed,
    )
    assert result.final_output == "done"
    assert result.context_wrapper._budget is not None
    assert result.context_wrapper._budget.tokens == 2
    assert result.context_wrapper._budget.requests == 2


@pytest.mark.asyncio
async def test_terminal_failed_event_is_observed_once_when_opted_in():
    response = get_response_obj([], usage=Usage(requests=1, total_tokens=9))
    model = ScriptedModel(
        [
            ModelStep.stream(
                [ResponseFailedEvent(type="response.failed", sequence_number=0, response=response)]
            )
        ]
    )
    with pytest.raises(ModelBehaviorError) as caught:
        await execute(
            Agent(name="root", model=model),
            RunBudget(max_tokens=50, include_failed_attempt_usage=True),
            True,
        )
    data = caught.value.run_data
    assert data is not None and data.context_wrapper._budget is not None
    assert data.context_wrapper._budget.tokens == data.context_wrapper.usage.total_tokens == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("include_failed", [False, True])
async def test_inherited_child_failure_usage_respects_ancestor_opt_in(streamed, include_failed):
    child_model = ScriptedModel(
        [
            failure(100),
            ModelStep(output=[get_text_message("child")], usage=Usage(requests=1, total_tokens=1)),
        ]
    )
    child = Agent(name="child", model=child_model, model_settings=retry_settings()).as_tool(
        "ask", "Ask child", failure_error_function=None
    )
    parent_model = ScriptedModel(
        [
            ModelStep(
                output=[get_function_tool_call("ask", '{"input":"go"}')],
                usage=Usage(requests=1, total_tokens=1),
            ),
            ModelStep(output=[get_text_message("done")], usage=Usage(requests=1, total_tokens=1)),
        ]
    )
    root = Agent(name="root", model=parent_model, tools=[child])
    budget = RunBudget(
        max_tokens=50, share_with_children=True, include_failed_attempt_usage=include_failed
    )
    if include_failed:
        with pytest.raises(RunBudgetError) as caught:
            await execute(root, budget, streamed)
        assert caught.value.limit == "tokens"
        data = caught.value.run_data
        assert data is not None and data.context_wrapper._budget is not None
        assert data.context_wrapper._budget.tokens == 102
        assert data.context_wrapper._budget.requests == 2
        assert len(parent_model.calls) == 1
    else:
        result = await execute(root, budget, streamed)
        assert result.final_output == "done" and result.context_wrapper._budget is not None
        assert result.context_wrapper._budget.tokens == 3
        assert len(parent_model.calls) == 2
    assert len(child_model.calls) == 2
