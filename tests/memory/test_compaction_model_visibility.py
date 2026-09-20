from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents import Agent, ModelSettings, RunConfig, Runner, RunState, handoff
from agents.handoffs import HandoffInputData
from agents.items import TResponseInputItem
from agents.memory import OpenAIResponsesCompactionSession, Session, SQLiteSession
from agents.memory.session import SessionABC
from agents.result import RunResult, RunResultStreaming
from agents.run_config import CallModelData, ModelInputData
from agents.testing import ScriptedModel
from tests.test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)
from tests.utils.simple_session import SimpleListSession

pytestmark = pytest.mark.asyncio

LOCAL_OUTPUT = "synthetic-local-only-result"


async def run(
    agent: Agent[Any],
    prompt: str | RunState[Any],
    session: Session,
    streamed: bool,
    config: RunConfig | None = None,
) -> RunResult | RunResultStreaming:
    if not streamed:
        return await Runner.run(agent, prompt, session=session, run_config=config)
    result = Runner.run_streamed(agent, prompt, session=session, run_config=config)
    async for _ in result.stream_events():
        pass
    return result


def without_tools(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    return [
        item for item in items if item.get("type") not in {"function_call", "function_call_output"}
    ]


def filter_tools(data: CallModelData[Any]) -> ModelInputData:
    return ModelInputData(
        input=without_tools(data.model_data.input), instructions=data.model_data.instructions
    )


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("filter_kind", ["model", "session", "redact"])
async def test_automatic_compaction_retains_hidden_tool_output_until_full_replay(
    streamed: bool, filter_kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = MagicMock()
    summary = {"type": "compaction", "id": "cmp-test", "encrypted_content": "synthetic-summary"}
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "visibility", SimpleListSession(), client=client, should_trigger_compaction=lambda _: True
    )
    model = ScriptedModel(
        steps=[
            [get_function_tool_call("lookup")],
            [get_text_message("ok")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="worker",
        model=model,
        model_settings=ModelSettings(store=False),
        tools=[get_function_tool(name="lookup", return_value=LOCAL_OUTPUT)],
        tool_use_behavior="stop_on_first_tool",
    )
    await run(agent, "first", session, streamed)
    client.responses.compact.assert_not_awaited()

    def session_filter(
        history: list[TResponseInputItem], new_input: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        return without_tools(history) + new_input

    def redact(data: CallModelData[Any]) -> ModelInputData:
        items = [
            cast(TResponseInputItem, {**item, "output": "redacted"})
            if item.get("type") == "function_call_output"
            else item
            for item in data.model_data.input
        ]
        return ModelInputData(input=items, instructions=data.model_data.instructions)

    config = (
        RunConfig(session_input_callback=session_filter)
        if filter_kind == "session"
        else RunConfig(call_model_input_filter=redact if filter_kind == "redact" else filter_tools)
    )
    await run(agent, "second", session, streamed, config)
    assert LOCAL_OUTPUT not in str(model.calls[1].input)
    client.responses.compact.assert_not_awaited()
    assert LOCAL_OUTPUT in str(await session.get_items())
    assert "Session history was retained" in caplog.text
    assert LOCAL_OUTPUT not in caplog.text

    await run(agent, "third", session, streamed)
    assert LOCAL_OUTPUT in str(model.calls[2].input)
    client.responses.compact.assert_awaited_once()
    assert LOCAL_OUTPUT in str(client.responses.compact.call_args.kwargs["input"])
    assert await session.get_items() == [summary]


@pytest.mark.parametrize("mode", ["input", "previous_response_id"])
@pytest.mark.parametrize("with_wrapper", [False, True])
async def test_manual_compaction_can_explicitly_replace_filtered_history(
    mode: Literal["input", "previous_response_id"], with_wrapper: bool
) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    history: list[TResponseInputItem] = [{"role": "user", "content": LOCAL_OUTPUT}]
    session = OpenAIResponsesCompactionSession(
        "manual",
        SimpleListSession(history=history),
        client=client,
        compaction_mode=mode,
        should_trigger_compaction=lambda _: True,
    )
    model = ScriptedModel(steps=[[get_text_message("ok")]])
    result = await run(
        Agent(name="worker", model=model),
        "visible",
        session,
        False,
        RunConfig(session_input_callback=lambda _history, new: new),
    )
    client.responses.compact.assert_not_awaited()
    assert LOCAL_OUTPUT in str(await session.get_items())
    await session.run_compaction(
        {"force": True}, wrapper=result.context_wrapper if with_wrapper else None
    )
    client.responses.compact.assert_awaited_once()
    if mode == "input":
        assert LOCAL_OUTPUT in str(client.responses.compact.call_args.kwargs["input"])
    assert await session.get_items() == []


@pytest.mark.parametrize("streamed", [False, True])
async def test_handoff_filter_cannot_expose_omitted_history_through_compaction(
    streamed: bool,
) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession(
        "handoff",
        SimpleListSession(),
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    target_model = ScriptedModel(steps=[[get_text_message("done")]])
    target = Agent(name="target", model=target_model)

    def omit_history(data: HandoffInputData) -> HandoffInputData:
        return replace(data, input_history=())

    source = Agent(
        name="source",
        model=ScriptedModel(steps=[[get_handoff_tool_call(target)]]),
        handoffs=[handoff(target, input_filter=omit_history)],
    )
    await run(source, LOCAL_OUTPUT, session, streamed)
    assert LOCAL_OUTPUT not in str(target_model.calls[0].input)
    client.responses.compact.assert_not_awaited()
    assert LOCAL_OUTPUT in str(await session.get_items())


async def test_encrypted_session_checks_decrypted_history() -> None:
    pytest.importorskip("cryptography")
    from agents.extensions.memory.encrypt_session import EncryptedSession

    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    backend = SimpleListSession()
    compaction = OpenAIResponsesCompactionSession(
        "encrypted",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    session = EncryptedSession("encrypted", compaction, encryption_key="synthetic-test-key")
    await session.add_items([{"role": "user", "content": LOCAL_OUTPUT}])
    model = ScriptedModel(steps=[[get_text_message("ok")], [get_text_message("done")]])
    agent = Agent(name="worker", model=model)
    await run(
        agent, "visible", session, True, RunConfig(session_input_callback=lambda _h, new: new)
    )
    client.responses.compact.assert_not_awaited()
    assert LOCAL_OUTPUT in str(await session.get_items())
    assert LOCAL_OUTPUT not in str(await backend.get_items())
    await run(agent, "full replay", session, True)
    client.responses.compact.assert_awaited_once()
    assert LOCAL_OUTPUT in str(client.responses.compact.call_args.kwargs["input"])


@pytest.mark.parametrize("streamed", [False, True])
async def test_resumed_tool_output_stays_local_when_filtered(streamed: bool) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession(
        "resume",
        SimpleListSession(),
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    tool = get_function_tool(name="lookup", return_value=LOCAL_OUTPUT)
    tool.needs_approval = True
    model = ScriptedModel(steps=[[get_function_tool_call("lookup")], [get_text_message("ok")]])
    agent = Agent(name="worker", model=model, tools=[tool], tool_use_behavior="stop_on_first_tool")
    # Avoid compacting the interruption's still-pending tool call.
    session.should_trigger_compaction = lambda _: False
    interrupted = await run(agent, "first", session, streamed)
    state = interrupted.to_state()
    state.approve(interrupted.interruptions[0])
    serialized = state.to_json()
    assert "_session_compaction_model_items" not in str(serialized)
    restored = await RunState.from_json(agent, serialized)
    session.should_trigger_compaction = lambda _: True
    resumed = await run(agent, restored, session, streamed)
    assert resumed.final_output == LOCAL_OUTPUT
    await run(agent, "second", session, streamed, RunConfig(call_model_input_filter=filter_tools))
    assert LOCAL_OUTPUT not in str(model.calls[-1].input)
    client.responses.compact.assert_not_awaited()
    assert LOCAL_OUTPUT in str(await session.get_items())


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("encrypted", [False, True])
async def test_backend_read_limit_cannot_hide_retained_history_from_compaction(
    streamed: bool, encrypted: bool
) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    backend = SQLiteSession("limited", session_settings={"limit": 1})
    session: SessionABC = OpenAIResponsesCompactionSession(
        "limited",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    if encrypted:
        pytest.importorskip("cryptography")
        from agents.extensions.memory.encrypt_session import EncryptedSession

        session = EncryptedSession("limited", session, encryption_key="synthetic-test-key")
    try:
        await session.add_items(
            [
                {"role": "user", "content": LOCAL_OUTPUT},
                {"role": "user", "content": "visible tail"},
            ]
        )
        model = ScriptedModel(steps=[[get_text_message("ok")], [get_text_message("done")]])
        agent = Agent(name="worker", model=model)
        await run(agent, "limited read", session, streamed)
        assert LOCAL_OUTPUT not in str(model.calls[0].input)
        client.responses.compact.assert_not_awaited()
        assert LOCAL_OUTPUT in str(await session.get_items(limit=100))

        await run(
            agent,
            "full read",
            session,
            streamed,
            RunConfig(session_settings={"limit": 100}),
        )
        assert LOCAL_OUTPUT in str(model.calls[1].input)
        client.responses.compact.assert_awaited_once()
        assert LOCAL_OUTPUT in str(client.responses.compact.call_args.kwargs["input"])
        assert await session.get_items(limit=100) == []
    finally:
        backend.close()
