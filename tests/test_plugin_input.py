import asyncio
from dataclasses import replace
from typing import Any, cast

import pytest

from agents import Agent, RunConfig, Runner, UserError, function_tool
from agents.plugins import Plugin, PluginHooks, RequireApproval
from agents.run_config import CallModelData, ModelInputData
from agents.testing import ScriptedModel, assistant_message, function_call


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_input_filters_are_detached_ordered_and_precede_the_app(streamed: bool) -> None:
    model = ScriptedModel([[assistant_message("ok")]])
    saved: list[Any] = []
    items: list[Any] = [
        {"role": "user", "content": "stale"},
        {"role": "user", "content": [{"type": "input_text", "text": "keep"}]},
    ]

    def prune(data: ModelInputData) -> ModelInputData:
        assert data.instructions == "base" and not hasattr(data, "context")
        saved.append(data.input[1])
        return replace(data, input=data.input[1:])

    async def guide(data: ModelInputData) -> ModelInputData:
        saved[0]["content"][0]["text"] = "late mutation"
        assert data.input == items[1:]
        return replace(data, instructions="edited")

    def final(data: CallModelData[Any]) -> ModelInputData:
        assert data.context == "private" and data.model_data.instructions == "edited"
        return replace(data.model_data, instructions="final")

    agent = Agent(
        "guide",
        model=model,
        instructions="base",
        plugins=[
            Plugin(name="prune", hooks=PluginHooks(filter_input=prune)),
            Plugin(name="guide", hooks=PluginHooks(filter_input=guide)),
        ],
    )
    config = RunConfig(call_model_input_filter=final)
    if streamed:
        result = Runner.run_streamed(agent, items, context="private", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, items, context="private", run_config=config)
    assert items[1]["content"][0]["text"] == "keep"
    assert model.calls[0].input == items[1:] and model.calls[0].system_instructions == "final"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["remove", "edit"])
async def test_plugin_cannot_remove_or_change_an_approved_call(change: str) -> None:
    @function_tool
    def fetch(key: str) -> str:
        return key

    def tamper(data: ModelInputData) -> ModelInputData:
        for item in data.input:
            if isinstance(item, dict) and item.get("type") == "function_call":
                if change == "remove":
                    data.input.remove(item)
                else:
                    cast(dict[str, Any], item)["arguments"] = '{"key":"two"}'
                break
        return data

    model = ScriptedModel([[function_call("fetch", {"key": "one"}, call_id="fetch1")]])
    plugin = Plugin(
        name="review",
        tools=[fetch],
        hooks=PluginHooks(
            check_tool=lambda call: RequireApproval(),
            filter_input=tamper,
        ),
    )
    agent = Agent("review", model=model, plugins=[plugin])
    step = await Runner.run(agent, "fetch")
    state = step.to_state()
    state.approve(step.interruptions[0])
    with pytest.raises(UserError, match="only remove user messages"):
        await Runner.run(agent, state)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_input_filter_rejects_partial_history_before_callback_or_model() -> None:
    seen = []

    def remember(data: ModelInputData) -> ModelInputData:
        seen.append(data)
        return data

    model = ScriptedModel()
    agent = Agent(
        "a", model=model, plugins=[Plugin(name="p", hooks=PluginHooks(filter_input=remember))]
    )
    with pytest.raises(UserError, match="client-managed history"):
        await Runner.run(agent, "start", previous_response_id="resp_prior")
    assert not seen and not model.calls


@pytest.mark.asyncio
async def test_failed_input_filter_does_not_expose_its_exception_or_send_input() -> None:
    class FalsyFilter:
        def __bool__(self) -> bool:
            return False

        def __call__(self, data: ModelInputData) -> ModelInputData:
            raise ValueError("private-data")

    class Hooks(PluginHooks):
        def __bool__(self) -> bool:
            return False

    model = ScriptedModel()
    agent = Agent(
        "a", model=model, plugins=[Plugin(name="p", hooks=Hooks(filter_input=FalsyFilter()))]
    )
    with pytest.raises(UserError, match="plugin callback failed") as error:
        await Runner.run(agent, "start")
    assert "private-data" not in str(error.value) and not model.calls


@pytest.mark.asyncio
async def test_plugin_instructions_are_redacted_before_the_next_filter_and_model() -> None:
    def first(data: ModelInputData) -> ModelInputData:
        return replace(data, instructions="sensitive")

    def second(data: ModelInputData) -> ModelInputData:
        assert data.instructions == "scrubbed"
        return replace(data, instructions="sensitive")

    def final(data: CallModelData[Any]) -> ModelInputData:
        assert data.model_data.instructions == "scrubbed"
        return replace(data.model_data, instructions="sensitive")

    model = ScriptedModel([[assistant_message("ok")]])
    agent = Agent(
        "a",
        model=model,
        plugins=[
            Plugin(name="first", hooks=PluginHooks(filter_input=first)),
            Plugin(name="second", hooks=PluginHooks(filter_input=second)),
        ],
    )
    await Runner.run(
        agent,
        "start",
        run_config=RunConfig(
            call_model_input_filter=final,
            redact_text=lambda s: s.replace("sensitive", "scrubbed"),
        ),
    )
    assert model.calls[0].system_instructions == "scrubbed"


@pytest.mark.asyncio
async def test_concurrent_plugin_filters_capture_reused_proposals_before_yielding() -> None:
    ready = asyncio.Event()
    calls = 0
    shared = ModelInputData(input=[], instructions=None)

    async def filter_input(data: ModelInputData) -> ModelInputData:
        nonlocal calls
        calls += 1
        if calls == 1:
            await ready.wait()
        else:
            ready.set()
        shared.input = data.input
        shared.instructions = data.instructions
        return shared

    plugin = Plugin(name="filter", hooks=PluginHooks(filter_input=filter_input))
    models = [ScriptedModel([[assistant_message("ok")]]) for _ in range(2)]
    agents = [
        Agent(name=name, instructions=name, model=model, plugins=[plugin])
        for name, model in zip(("left", "right"), models, strict=True)
    ]
    await asyncio.wait_for(
        asyncio.gather(*(Runner.run(agent, "same input") for agent in agents)), timeout=5
    )
    assert [model.calls[0].system_instructions for model in models] == ["left", "right"]
