"""Cross-feature acceptance at SDK admission and dispatch boundaries."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    RunBudget,
    RunBudgetError,
    RunConfig,
    RunControl,
    Runner,
    Usage,
    UserError,
)
from agents.decorators import tool
from agents.models.interface import ModelProvider
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession

pytestmark = pytest.mark.asyncio


class RecordingProvider(ModelProvider):
    def __init__(self, models):
        self.models = models
        self.names = []

    def get_model(self, name):
        self.names.append(name)
        return self.models[name]


async def drive(streamed: bool, agent: Agent[Any], **kwargs: Any) -> Any:
    if not streamed:
        return await Runner.run(agent, "start", **kwargs)
    result = Runner.run_streamed(agent, "start", **kwargs)
    try:
        async for _ in result.stream_events():
            pass
        return result
    finally:
        if not result.is_complete:
            result.cancel()


async def stop(task: asyncio.Task[Any], release: asyncio.Event) -> None:
    release.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def text(item: Any) -> str | None:
    if not isinstance(item, dict) or item.get("role") != "user":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def count_message(items: list[Any], message: str) -> int:
    return sum(text(item) == message for item in items)


@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_request_budget_denial_after_live_admission_preserves_truthful_receipt(
    streamed: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool
    async def failing_test() -> str:
        entered.set()
        await release.wait()
        effects.append("test failed")
        return "test failed"

    first = ScriptedModel([[function_call("failing_test", "{}", call_id="test")]])
    stronger = ScriptedModel([[assistant_message("must not dispatch")]])
    provider = RecordingProvider({"base": first, "stronger": stronger})
    selections = []

    def select(data):
        selections.append(data.turn_number)
        return "stronger" if data.turn_number == 2 else None

    agent = Agent(name="worker", model="base", tools=[failing_test])
    control, session = RunControl(), SimpleListSession()
    config = RunConfig(
        model_provider=provider,
        model_selector=select,
        budget=RunBudget(max_outer_requests=1),
        tracing_disabled=True,
    )
    task = asyncio.create_task(
        drive(streamed, agent, control=control, session=session, run_config=config)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        receipt = control.send("Explain before editing.")
        release.set()
        with pytest.raises(RunBudgetError) as caught:
            await asyncio.wait_for(task, 3)
        assert caught.value.limit == "outer_requests"
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is True
        assert len(first.calls) == 1 and not stronger.calls
        assert effects == ["test failed"] and agent.model == "base"
        assert count_message(await session.get_items(), "Explain before editing.") == 1
        assert selections[0] == 1
        with pytest.raises(UserError):
            control.send("after budget failure")
    finally:
        await stop(task, release)


@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_selected_model_observes_tokens_and_later_budget_failure_preserves_receipt(
    streamed: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool
    async def advance() -> str:
        entered.set()
        await release.wait()
        effects.append("advanced")
        return "next"

    usage = Usage(input_tokens=1, output_tokens=1, total_tokens=2)
    first = ScriptedModel(
        [
            ModelStep(
                output=[function_call("advance", "{}", call_id="first")],
                usage=usage,
            )
        ]
    )
    second = ScriptedModel(
        [
            ModelStep(
                output=[function_call("advance", "{}", call_id="second")],
                usage=usage,
            )
        ]
    )
    provider = RecordingProvider({"base": first, "stronger": second})

    control = RunControl()
    agent = Agent(name="worker", model="base", tools=[advance])
    config = RunConfig(
        model_provider=provider,
        model_selector=lambda data: "stronger" if data.turn_number == 2 else None,
        budget=RunBudget(max_tokens=3),
        tracing_disabled=True,
    )
    task = asyncio.create_task(drive(streamed, agent, control=control, run_config=config))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        receipt = control.send("Use the stronger model to explain.")
        release.set()
        with pytest.raises(RunBudgetError) as caught:
            await asyncio.wait_for(task, 3)
        assert caught.value.limit == "tokens"
        assert len(first.calls) == len(second.calls) == 1
        assert effects == ["advanced", "advanced"]
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "submitted" and outcome.turn_number == 2
        assert count_message(second.calls[0].input, "Use the stronger model to explain.") == 1
    finally:
        await stop(task, release)


@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_selected_adapter_preflight_rejects_before_receipt_submission_or_network(
    streamed: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    network_requests = []

    def unexpected_request(request):
        network_requests.append(request)
        raise AssertionError("Known adapter incompatibility reached the transport.")

    client = AsyncOpenAI(
        api_key="synthetic-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(unexpected_request)),
    )
    selected = OpenAIChatCompletionsModel(
        model="fixture-chat", openai_client=client, strict_feature_validation=True
    )

    @tool
    async def failing_test() -> str:
        entered.set()
        await release.wait()
        return "test failed"

    first = ScriptedModel([[function_call("failing_test", "{}", call_id="test")]])
    provider = RecordingProvider({"base": first, "chat": selected})
    agent = Agent(
        name="worker",
        model="base",
        tools=[failing_test],
        prompt={"id": "pmpt_synthetic"},
    )
    control, session = RunControl(), SimpleListSession()
    config = RunConfig(
        model_provider=provider,
        model_selector=lambda data: "chat" if data.turn_number == 2 else None,
        budget=RunBudget(max_outer_requests=2),
        tracing_disabled=True,
    )
    task = asyncio.create_task(
        drive(streamed, agent, control=control, session=session, run_config=config)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        receipt = control.send("Explain before editing.")
        release.set()
        with pytest.raises(UserError, match="Chat Completions|prompt"):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is True
        assert provider.names == ["base", "chat"]
        assert network_requests == [] and len(first.calls) == 1
        assert count_message(await session.get_items(), "Explain before editing.") == 1
    finally:
        await stop(task, release)
        await client.close()


@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_nested_run_shares_last_tool_slot_without_consuming_parent_correction(
    streamed: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool
    async def write(value: str) -> str:
        effects.append(value)
        return value

    async def child_request(call):
        entered.set()
        await release.wait()
        return [function_call("write", '{"value":"left"}', call_id="left")]

    child_model = ScriptedModel(
        [
            ModelStep.respond(child_request),
            [function_call("write", '{"value":"right"}', call_id="right")],
        ]
    )
    child = Agent(name="child", model=child_model, tools=[write])
    delegate = child.as_tool("delegate", "Delegate", failure_error_function=None)
    parent_model = ScriptedModel(
        [
            [function_call("delegate", '{"input":"child question"}', call_id="delegate")],
        ]
    )
    parent = Agent(name="parent", model=parent_model, tools=[delegate])
    control = RunControl()
    config = RunConfig(
        budget=RunBudget(max_tool_starts=2, share_with_children=True),
        tracing_disabled=True,
    )
    task = asyncio.create_task(drive(streamed, parent, control=control, run_config=config))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        receipt = control.send("Parent-only correction.")
        release.set()
        with pytest.raises(RunBudgetError) as caught:
            await asyncio.wait_for(task, 3)
        assert caught.value.limit == "tool_starts"
        assert effects == ["left"]
        assert len(parent_model.calls) == 1 and len(child_model.calls) == 2
        assert all(
            count_message(call.input, "Parent-only correction.") == 0 for call in child_model.calls
        )
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is False
    finally:
        await stop(task, release)
