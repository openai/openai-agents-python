"""Exercise the relocated classic runner without model credentials or native Core."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents.classic import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    OpenAIResponsesCompactionSession,
    RunConfig,
    Runner,
    RunState,
    SessionSettings,
    SQLiteSession,
    handoff,
)
from agents.classic._run_state_agent_identity import _agent_identity_signature
from agents.classic.items import HandoffOutputItem, ToolCallOutputItem, TResponseInputItem
from agents.classic.memory.session import SessionABC

from .classic_support import (
    DeterministicModel,
    approval_graph,
    record_effect,
    text_message,
    tool_call,
)

CONFIG = RunConfig(tracing_disabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", ["custom", "sqlite"])
@pytest.mark.parametrize("settings", ["omitted", "unlimited", "wrapper_limit", "run_limit"])
async def test_classic_compaction_preserves_retrieval_settings(
    store_kind: str, settings: str
) -> None:
    class HistorySession(SessionABC):
        session_id = "compaction-settings"

        def __init__(self) -> None:
            self.session_settings = SessionSettings(limit=2)
            self.items: list[TResponseInputItem] = []

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            # SessionABC defines None as all items. Runner applies retrieval metadata
            # when this store is used directly; its wrapper has independent settings.
            return list(self.items if limit is None else self.items[-limit:])

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            self.items.extend(items)

        async def pop_item(self) -> TResponseInputItem | None:
            return self.items.pop() if self.items else None

        async def clear_session(self) -> None:
            self.items.clear()

    backing = (
        SQLiteSession("compaction-settings", session_settings=SessionSettings(limit=2))
        if store_kind == "sqlite"
        else HistorySession()
    )
    history: list[TResponseInputItem] = [
        {"role": "user", "content": "oldest"},
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "newest"},
    ]
    try:
        await backing.add_items(history)
        session = OpenAIResponsesCompactionSession(
            backing.session_id, backing, should_trigger_compaction=lambda _: False
        )
        if settings == "unlimited":
            session.session_settings = SessionSettings(limit=None)
        elif settings == "wrapper_limit":
            session.session_settings = SessionSettings(limit=1)
        config = (
            RunConfig(tracing_disabled=True, session_settings=SessionSettings(limit=1))
            if settings == "run_limit"
            else CONFIG
        )
        model = DeterministicModel([[text_message("answer")]])
        result = await Runner.run(
            Agent(name="compaction", model=model), "next", session=session, run_config=config
        )
        expected = history
        if settings in ("wrapper_limit", "run_limit"):
            expected = history[-1:]
        elif store_kind == "sqlite":
            expected = history[-2:]
        assert model.calls[0].input == [*expected, {"role": "user", "content": "next"}]
        assert result.final_output == "answer"
        assert backing.session_settings is not None
        assert backing.session_settings.limit == 2
        model.assert_complete()
    finally:
        if isinstance(backing, SQLiteSession):
            backing.close()


def test_classic_sync_run() -> None:
    model = DeterministicModel([[text_message("sync answer")]])
    result = Runner.run_sync(Agent(name="sync", model=model), "hello", run_config=CONFIG)
    assert result.final_output == "sync answer"
    assert len(model.calls) == 1
    model.assert_complete()


@pytest.mark.asyncio
async def test_classic_async_tool_executes_and_returns_to_model() -> None:
    model = DeterministicModel(
        [[tool_call("record_effect", "once", "tool-once")], [text_message("async answer")]]
    )
    context: dict[str, Any] = {"effects": []}
    result = await Runner.run(
        Agent(name="tools", model=model, tools=[record_effect]),
        "record once",
        context=context,
        run_config=CONFIG,
    )
    assert result.final_output == "async answer"
    assert context["effects"] == ["once"]
    assert any(
        isinstance(item, ToolCallOutputItem) and item.output == "recorded:once"
        for item in result.new_items
    )
    assert len(model.calls) == 2
    assert any(
        item.get("type") == "function_call_output" and item.get("output") == "recorded:once"
        for item in model.calls[-1].input
        if isinstance(item, dict)
    )
    model.assert_complete()


@pytest.mark.asyncio
async def test_classic_streamed_events_and_final_output() -> None:
    model = DeterministicModel([[text_message("streamed answer")]])
    result = Runner.run_streamed(Agent(name="stream", model=model), "hello", run_config=CONFIG)
    events = [event async for event in result.stream_events()]
    assert result.final_output == "streamed answer"
    assert result.is_complete
    assert any(event.type == "raw_response_event" for event in events)
    assert any(
        event.type == "run_item_stream_event" and event.name == "message_output_created"
        for event in events
    )
    model.assert_complete()


@pytest.mark.asyncio
async def test_classic_handoff_runs_the_destination() -> None:
    destination_model = DeterministicModel([[text_message("destination answer")]])
    destination = Agent(name="destination", model=destination_model)
    # Handoff tools accept an empty object rather than application tool arguments.
    source_model = DeterministicModel(
        [
            [
                tool_call("choose_destination", "unused", "handoff-destination").model_copy(
                    update={"arguments": "{}"}
                )
            ]
        ]
    )
    source = Agent(
        name="source",
        model=source_model,
        handoffs=[handoff(destination, tool_name_override="choose_destination")],
    )
    result = await Runner.run(source, "delegate", run_config=CONFIG)
    assert result.final_output == "destination answer"
    assert result.last_agent is destination
    assert any(isinstance(item, HandoffOutputItem) for item in result.new_items)
    source_model.assert_complete()
    destination_model.assert_complete()


@pytest.mark.asyncio
async def test_classic_input_guardrail_blocks_before_model() -> None:
    async def block(context, agent, input):
        return GuardrailFunctionOutput(output_info="blocked", tripwire_triggered=True)

    model = DeterministicModel([[text_message("must not run")]])
    agent = Agent(
        name="guarded",
        model=model,
        input_guardrails=[InputGuardrail(guardrail_function=block, run_in_parallel=False)],
    )
    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        await Runner.run(agent, "blocked input", run_config=CONFIG)
    assert exc_info.value.guardrail_result.output.output_info == "blocked"
    assert model.calls == ()


@pytest.mark.asyncio
async def test_original_paused_approval_resumes_with_correct_duplicate_agent() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/classic/original_pending_approval.json").read_text()
    )
    root, low, high = approval_graph(reverse=True)
    state = await RunState.from_json(root, payload)
    pending = state.get_interruptions()
    assert len(pending) == 1
    assert pending[0].agent is high
    assert pending[0].agent is not low
    state.approve(pending[0])
    result = await Runner.run(root, state, run_config=CONFIG)
    assert result.final_output == "resumed"
    assert result.last_agent is high
    assert result.context_wrapper.context["effects"] == ["before", "approved"]
    assert result.interruptions == []
    assert low.model.calls == ()
    assert isinstance(high.model, DeterministicModel)
    high.model.assert_complete()


def test_historical_agent_identity_signatures_survive_namespace_relocation() -> None:
    metadata = json.loads(
        (
            Path(__file__).parent / "fixtures/classic/original_pending_approval.identity.json"
        ).read_text()
    )
    root, low, high = approval_graph(reverse=True)
    assert metadata["runtime_module"] == "agents"
    # Ordinal identities can conceal a namespace change when sorting stays unchanged.
    # Compare the historical durable signature to cover that compatibility boundary.
    assert {
        label: _agent_identity_signature(agent)
        for label, agent in (("root", root), ("low", low), ("high", high))
    } == metadata["agent_signatures"]
