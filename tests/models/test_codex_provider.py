import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import Agent, RunConfig, Runner, UserError
from agents.items import TResponseInputItem
from agents.models.codex import CodexProvider
from agents.models.multi_provider import MultiProvider


@pytest.fixture
def native_turn(monkeypatch):
    # The fake replaces only the optional extension. The tests exercise the real Runner.
    run_turn = AsyncMock(
        return_value={
            "text": "A Codex response",
            "model": "gpt-5.3-codex",
            "item_id": "item_123",
            "turn_id": "turn_123",
            "input_tokens": 10,
            "cached_input_tokens": 3,
            "cache_write_input_tokens": 0,
            "output_tokens": 7,
            "reasoning_output_tokens": 2,
            "total_tokens": 17,
        }
    )
    monkeypatch.setitem(sys.modules, "_openai_agents_codex", SimpleNamespace(run_turn=run_turn))
    return run_turn


@pytest.mark.asyncio
async def test_codex_provider_runs_through_runner_and_replays_text_history(tmp_path, native_turn):
    config = RunConfig(model_provider=CodexProvider(cwd=tmp_path), tracing_disabled=True)
    agent = Agent(name="test", instructions="Be brief.", model="gpt-5.3-codex")
    result = await Runner.run(agent, "Start here.", run_config=config)
    assert result.final_output == "A Codex response"
    assert result.context_wrapper.usage.total_tokens == 17
    native_turn.assert_awaited_once_with(
        "Start here.", "Be brief.", "gpt-5.3-codex", str(tmp_path), []
    )

    history = result.to_input_list()
    history.append({"role": "user", "content": "What follows?"})
    streamed = Runner.run_streamed(agent, history, run_config=config)
    events = [event async for event in streamed.stream_events()]
    assert streamed.final_output == result.final_output
    assert streamed.context_wrapper.usage == result.context_wrapper.usage
    assert [
        event.data.delta
        for event in events
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta"
    ] == ["A Codex response"]
    native_turn.assert_awaited_with(
        "What follows?",
        "Be brief.",
        "gpt-5.3-codex",
        str(tmp_path),
        [("user", "Start here."), ("assistant", "A Codex response")],
    )


@pytest.mark.asyncio
async def test_codex_provider_rejects_multimodal_input_before_launch(native_turn):
    config = RunConfig(model_provider=CodexProvider(), tracing_disabled=True)
    image_input: list[TResponseInputItem] = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "https://example.com/a.png", "detail": "auto"}
            ],
        }
    ]
    with pytest.raises(UserError, match="only user and assistant text messages"):
        await Runner.run(Agent(name="test"), image_input, run_config=config)
    native_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_provider_reports_missing_extension_at_use(monkeypatch):
    monkeypatch.setitem(sys.modules, "_openai_agents_codex", None)
    assert isinstance(RunConfig().model_provider, MultiProvider)
    config = RunConfig(model_provider=CodexProvider(), tracing_disabled=True)
    with pytest.raises(UserError, match="Install the Codex extension"):
        await Runner.run(Agent(name="test"), "Hello", run_config=config)
