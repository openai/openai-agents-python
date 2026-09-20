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
from tests.utils.simple_session import IdStrippingSession, SimpleListSession

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
async def test_automatic_compaction_respects_backend_id_matching_policy(streamed: bool) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    backend = IdStrippingSession()
    duplicate: TResponseInputItem = {"role": "user", "content": "repeated request"}
    await backend.add_items([duplicate, duplicate])
    session = OpenAIResponsesCompactionSession(
        "id-stripping",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    model = ScriptedModel(steps=[[get_text_message("ok")], [get_text_message("done")]])
    agent = Agent(name="worker", model=model)
    await run(
        agent,
        "filtered",
        session,
        streamed,
        RunConfig(session_input_callback=lambda history, new: history[1:] + new),
    )
    client.responses.compact.assert_not_awaited()
    assert (await session.get_items()).count(duplicate) == 2
    assert all("id" not in item for item in await session.get_items())

    await run(agent, "complete", session, streamed)
    client.responses.compact.assert_awaited_once()
    compact_input = client.responses.compact.call_args.kwargs["input"]
    assert compact_input.count(duplicate) == 2
    assert "done" in str(compact_input)
    assert await session.get_items() == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
async def test_automatic_compaction_hook_approves_the_reloaded_snapshot(
    streamed: bool,
    encrypted: bool,
    deferred: bool,
) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    backend = SQLiteSession("hook-snapshot", session_settings={"limit": 1})
    approved_views: list[list[TResponseInputItem]] = []

    def approve(context: Any) -> bool:
        items = context["session_items"]
        approved_views.append(list(items))
        return LOCAL_OUTPUT not in str(items)

    compaction = OpenAIResponsesCompactionSession(
        "hook-snapshot",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=approve,
    )
    session: SessionABC = compaction
    if encrypted:
        pytest.importorskip("cryptography")
        from agents.extensions.memory.encrypt_session import EncryptedSession

        session = EncryptedSession("hook-snapshot", compaction, encryption_key="synthetic-test-key")
    try:
        await session.add_items(
            [
                {"role": "user", "content": LOCAL_OUTPUT},
                {"role": "user", "content": "visible tail"},
            ]
        )
        model = ScriptedModel(
            steps=([[get_function_tool_call("lookup")]] if deferred else [])
            + [[get_text_message("ok")]]
        )
        await run(
            Agent(
                name="worker",
                model=model,
                tools=[get_function_tool(name="lookup", return_value="tool result")],
            ),
            "full read",
            session,
            streamed,
            RunConfig(session_settings={"limit": 100}),
        )
        assert LOCAL_OUTPUT in str(model.calls[-1].input)
        assert LOCAL_OUTPUT not in str(approved_views[0])
        client.responses.compact.assert_not_awaited()
        assert LOCAL_OUTPUT in str(approved_views[-1])
        assert LOCAL_OUTPUT in str(await session.get_items(limit=100))
        if deferred:
            assert compaction._get_deferred_compaction_response_id() is not None
    finally:
        backend.close()


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


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("encryption", ["none", "outer", "inner"])
async def test_automatic_compaction_does_not_reload_unbounded_hidden_history(
    approve: bool, encryption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    backend = SQLiteSession("bounded", session_settings={"limit": 1})
    session: SessionABC = OpenAIResponsesCompactionSession(
        "bounded",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: approve,
    )
    clock = [1000]
    if encryption != "none":
        pytest.importorskip("cryptography")
        from agents.extensions.memory.encrypt_session import EncryptedSession

        monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
        if encryption == "outer":
            session = EncryptedSession(
                "bounded", session, encryption_key="synthetic-test-key", ttl=10
            )
        else:
            session = OpenAIResponsesCompactionSession(
                "bounded",
                EncryptedSession("bounded", backend, encryption_key="synthetic-test-key", ttl=10),
                client=client,
                compaction_mode="input",
                should_trigger_compaction=lambda _: approve,
            )
    try:
        await session.add_items(
            [{"role": "user", "content": f"retained-{index}"} for index in range(100)]
        )
        if encryption != "none":
            clock[0] += 11
            await session.add_items([{"role": "user", "content": "fresh tail"}])
        read = AsyncMock(wraps=backend.get_items)
        backend.get_items = read  # type: ignore[method-assign]
        model = ScriptedModel(steps=[[get_text_message("ok")], [get_text_message("done")]])
        agent = Agent(name="worker", model=model)
        for prompt in ("first", "second"):
            read.reset_mock()
            await run(agent, prompt, session, False)
            # One stored input, one new prompt, one response, and one lookahead item.
            limits = [
                call.kwargs.get("limit", call.args[0] if call.args else None)
                for call in read.call_args_list
            ]
            assert all(limit is None or limit <= 4 for limit in limits)
            if not approve:
                assert all(limit is None or limit == 1 for limit in limits)
            client.responses.compact.assert_not_awaited()
        assert len(await backend.get_items(limit=1000)) == (105 if encryption != "none" else 104)
        # Explicit manual compaction can still process the application's approved
        # logical history, including when limited reads omit live history.
        await session.run_compaction({"force": True})  # type: ignore[attr-defined]
        client.responses.compact.assert_awaited_once()
        assert "done" in str(client.responses.compact.call_args.kwargs["input"])
        assert await backend.get_items(limit=1000) == []
    finally:
        backend.close()


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mode", ["input", "previous_response_id"])
async def test_automatic_compaction_preserves_filtered_duplicate_occurrences(
    streamed: bool, mode: Literal["input", "previous_response_id"]
) -> None:
    duplicate: TResponseInputItem = {"role": "user", "content": "repeated request"}
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession(
        "duplicates",
        SimpleListSession(history=[duplicate, duplicate]),
        client=client,
        compaction_mode=mode,
        should_trigger_compaction=lambda _: True,
    )
    model = ScriptedModel(steps=[[get_text_message("ok")], [get_text_message("done")]])
    agent = Agent(name="worker", model=model)
    await run(
        agent,
        "filtered",
        session,
        streamed,
        RunConfig(session_input_callback=lambda history, new: history[1:] + new),
    )
    assert model.calls[0].input.count(duplicate) == 1
    client.responses.compact.assert_not_awaited()
    assert (await session.get_items()).count(duplicate) == 2

    await run(agent, "complete", session, streamed)
    assert model.calls[1].input.count(duplicate) == 2
    client.responses.compact.assert_awaited_once()
    if mode == "input":
        assert client.responses.compact.call_args.kwargs["input"].count(duplicate) == 2
    assert await session.get_items() == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mode", ["input", "previous_response_id"])
@pytest.mark.parametrize("filter_kind", ["model", "session"])
async def test_automatic_compaction_preserves_reordered_history(
    streamed: bool, mode: Literal["input", "previous_response_id"], filter_kind: str
) -> None:
    first: TResponseInputItem = {"role": "user", "content": "first stored request"}
    second: TResponseInputItem = {"role": "user", "content": "second stored request"}
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession(
        "ordered",
        SimpleListSession(history=[first, second]),
        client=client,
        compaction_mode=mode,
        should_trigger_compaction=lambda _: True,
    )
    model = ScriptedModel(steps=[[get_text_message("ok")], [get_text_message("done")]])
    agent = Agent(name="worker", model=model)

    def reorder(data: CallModelData[Any]) -> ModelInputData:
        items = data.model_data.input
        return ModelInputData(
            input=[items[1], items[0], *items[2:]], instructions=data.model_data.instructions
        )

    config = (
        RunConfig(call_model_input_filter=reorder)
        if filter_kind == "model"
        else RunConfig(session_input_callback=lambda history, new: [*reversed(history), *new])
    )
    await run(agent, "reordered", session, streamed, config)
    assert model.calls[0].input[:2] == [second, first]
    client.responses.compact.assert_not_awaited()
    assert (await session.get_items())[:2] == [first, second]

    model_only: TResponseInputItem = {"role": "user", "content": "model-only context"}

    def add_context(data: CallModelData[Any]) -> ModelInputData:
        items = data.model_data.input
        return ModelInputData(
            input=[items[0], model_only, *items[1:]], instructions=data.model_data.instructions
        )

    await run(agent, "ordered", session, streamed, RunConfig(call_model_input_filter=add_context))
    assert model.calls[1].input[:3] == [first, model_only, second]
    client.responses.compact.assert_awaited_once()
    if mode == "input":
        compact_input = client.responses.compact.call_args.kwargs["input"]
        assert compact_input[:2] == [first, second]
        assert model_only not in compact_input
    assert await session.get_items() == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("limited_default", [False, True])
async def test_automatic_compaction_recovers_after_encrypted_history_expires(
    streamed: bool, outer: bool, limited_default: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography")
    from agents.extensions.memory.encrypt_session import EncryptedSession

    clock = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
    backend = SQLiteSession("expiry", session_settings={"limit": 20} if limited_default else None)
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session: SessionABC
    if outer:
        session = EncryptedSession(
            "expiry",
            OpenAIResponsesCompactionSession(
                "expiry", backend, client=client, compaction_mode="input"
            ),
            encryption_key="synthetic-test-key",
            ttl=10,
        )
    else:
        session = OpenAIResponsesCompactionSession(
            "expiry",
            EncryptedSession("expiry", backend, encryption_key="synthetic-test-key", ttl=10),
            client=client,
            compaction_mode="input",
        )
    model = ScriptedModel(steps=[[get_text_message("ok")] for _ in range(15)])
    agent = Agent(name="worker", model=model)
    config = RunConfig(session_settings={"limit": 100}) if limited_default else None
    try:
        await session.add_items([{"role": "user", "content": "expired request"}])
        clock[0] += 11
        for index in range(15):
            await run(agent, f"turn {index}", session, streamed, config)
        client.responses.compact.assert_awaited_once()
        assert len(client.responses.compact.call_args.kwargs["input"]) == 20
        assert "expired request" not in str(client.responses.compact.call_args.kwargs["input"])
        assert len(await backend.get_items(limit=100)) == 10
    finally:
        backend.close()


@pytest.mark.parametrize("outer", [False, True])
async def test_expired_read_overhead_does_not_authorize_hidden_history(
    outer: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography")
    from agents.extensions.memory.encrypt_session import EncryptedSession

    clock = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
    backend = SQLiteSession("expiry-hidden", session_settings={"limit": 1})
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session: SessionABC
    if outer:
        session = EncryptedSession(
            backend.session_id,
            OpenAIResponsesCompactionSession(
                backend.session_id,
                backend,
                client=client,
                compaction_mode="input",
                should_trigger_compaction=lambda _: True,
            ),
            encryption_key="synthetic-test-key",
            ttl=10,
        )
    else:
        session = OpenAIResponsesCompactionSession(
            backend.session_id,
            EncryptedSession(
                backend.session_id, backend, encryption_key="synthetic-test-key", ttl=10
            ),
            client=client,
            compaction_mode="input",
            should_trigger_compaction=lambda _: True,
        )

    def hide_history(data: CallModelData[Any]) -> ModelInputData:
        return ModelInputData(
            input=[item for item in data.model_data.input if item.get("content") != LOCAL_OUTPUT]
            + [{"role": "user", "content": "model-only context"}],
            instructions=data.model_data.instructions,
        )

    model = ScriptedModel(steps=[[get_text_message("ok")] for _ in range(3)])
    agent = Agent(name="worker", model=model)
    try:
        await session.add_items([{"role": "user", "content": "expired"}] * 10)
        clock[0] += 11
        await session.add_items([{"role": "user", "content": LOCAL_OUTPUT}])
        await run(
            agent,
            "filtered",
            session,
            False,
            RunConfig(session_settings={"limit": 100}, call_model_input_filter=hide_history),
        )
        client.responses.compact.assert_not_awaited()
        assert LOCAL_OUTPUT not in str(model.calls[0].input)
        assert len(await backend.get_items(limit=100)) == 13

        read = AsyncMock(wraps=backend.get_items)
        backend.get_items = read  # type: ignore[method-assign]
        await run(agent, "bounded", session, False)
        # The previous full read's overhead is not carried into a new bounded turn.
        limits = [
            call.kwargs.get("limit", call.args[0] if call.args else None)
            for call in read.call_args_list
        ]
        assert all(limit is None or limit <= 4 for limit in limits)
        client.responses.compact.assert_not_awaited()
        await run(agent, "visible", session, False, RunConfig(session_settings={"limit": 100}))
        client.responses.compact.assert_awaited_once()
        assert LOCAL_OUTPUT in str(client.responses.compact.call_args.kwargs["input"])
        assert await backend.get_items(limit=100) == []
    finally:
        backend.close()
