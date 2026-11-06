"""Continue completed Classic Session history through the native runner."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.responses import ResponseOutputMessage, ResponseReasoningItem

from agents import LocalStorage, ReconciledCall
from agents.classic import (
    Agent,
    Model,
    ModelProvider,
    OpenAIConversationsSession,
    OpenAIResponsesCompactionSession,
    RunConfig,
    RunContextWrapper,
    Runner,
    Session,
    SessionSettings,
    SQLiteSession,
    function_tool,
)
from agents.classic.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from agents.classic.items import ModelResponse, TResponseInputItem
from agents.classic.models.fake_id import FAKE_RESPONSES_ID
from agents.classic.stream_events import AgentUpdatedStreamEvent
from agents.classic.tracing import setup as tracing_setup
from agents.classic.tracing.provider import DefaultTraceProvider
from agents.migration import SDKRunner
from tests.test_migration_runner import ScriptedModel, call, message


@pytest.fixture(autouse=True)
def local_transport_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def _new_message(text: str, message_id: str) -> ResponseOutputMessage:
    return message(text).model_copy(update={"id": message_id})


def _item_order(items: Sequence[Mapping[str, Any]]) -> list[str]:
    return [item.get("call_id") or item.get("id") or item["role"] for item in items]


async def _continue_in_new_process(path: Path) -> dict[str, Any]:
    test_package_parent = Path(__file__).resolve().parents[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.test_migration_session",
        str(path),
        cwd=path.parent,
        env={**os.environ, "PYTHONPATH": str(test_package_parent)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    result = json.loads(stdout)
    assert isinstance(result, dict)
    return result


async def _restart(path: str) -> None:
    @function_tool
    def lookup(value: str) -> str:
        raise AssertionError(f"historical lookup re-executed: {value}")

    stored = SQLiteSession("source", path)
    try:
        model = ScriptedModel([_new_message("Retained the previous facts.", "msg_restart")])
        result = await SDKRunner.run(
            Agent("Restarted", model=model, tools=[lookup]),
            "Use the previous facts",
            session=stored,
        )
        print(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "output": result.final_output,
                    "input_order": _item_order(model.requests[0]["input"]),
                    "stored_order": _item_order(await stored.get_items()),
                }
            )
        )
    finally:
        stored.close()


async def test_classic_sqlite_history_continues_and_survives_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.db"
    effects: list[str] = []

    def lookup(value: str) -> str:
        effects.append(value)
        return f"fact:{value}"

    source = SQLiteSession("source", database)
    isolated = SQLiteSession("isolated", database)
    try:
        reasoning = ResponseReasoningItem.model_validate(
            {
                "type": "reasoning",
                "id": "rs_classic",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "Look up the given name."}],
                "encrypted_content": "synthetic-signature",
            }
        )
        ada_call = call("classic_lookup", call_id="call_ada")
        grace_call = call("classic_lookup", call_id="call_grace", arguments='{"value":"Grace"}')
        classic_message = _new_message("Recorded Ada and Grace.", "msg_classic")
        classic_model = ScriptedModel([reasoning, ada_call, grace_call], [classic_message])
        classic_agent = Agent(
            "Classic",
            model=classic_model,
            tools=[function_tool(lookup, name_override="classic_lookup")],
        )
        with monkeypatch.context() as tracing_patch:
            # Classic resolves the tracing provider even when tracing is disabled.
            tracing_patch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", DefaultTraceProvider())
            await Runner.run(
                classic_agent,
                "Remember Ada and Grace",
                session=source,
                run_config=RunConfig(tracing_disabled=True),
            )
        original = await source.get_items()
        assert original == [
            {"role": "user", "content": "Remember Ada and Grace"},
            reasoning.model_dump(exclude_unset=True),
            ada_call.model_dump(exclude_unset=True),
            grace_call.model_dump(exclude_unset=True),
            {"type": "function_call_output", "call_id": "call_ada", "output": "fact:Ada"},
            {"type": "function_call_output", "call_id": "call_grace", "output": "fact:Grace"},
            classic_message.model_dump(exclude_unset=True),
        ]
        assert sorted(effects) == ["Ada", "Grace"]

        native_message = _new_message("Recorded Lina.", "msg_native").model_copy(
            update={"phase": "final_answer"}
        )
        native_call = call("lookup", call_id="call_native", arguments='{"value":"Lina"}')
        native_model = ScriptedModel([native_call], [native_message])
        fresh_text = "  Remember Lina.\r\nKeep the exact input.  "
        native = await SDKRunner.run(
            Agent("Native", model=native_model, tools=[function_tool(lookup)]),
            fresh_text,
            session=source,
        )
        assert native.final_output == "Recorded Lina."
        assert sorted(effects) == ["Ada", "Grace", "Lina"]
        first, follow_up = [request["input"] for request in native_model.requests]
        assert first[: len(original)] == original
        fresh = {"role": "user", "content": fresh_text}
        assert first[len(original) :] == [fresh]
        assert follow_up[: len(first)] == first
        assert _item_order(follow_up[len(first) :]) == ["call_native", "call_native"]

        output_delta = [
            native_call.model_dump(exclude_unset=True),
            {"type": "function_call_output", "call_id": "call_native", "output": "fact:Lina"},
            native_message.model_dump(exclude_unset=True),
        ]
        stored = await source.get_items()
        assert native.input == first
        assert native.to_input_list() == [*first, *output_delta]
        assert stored == [*original, fresh, *output_delta]

        isolated_model = ScriptedModel([_new_message("Separate answer.", "msg_isolated")])
        await SDKRunner.run(
            Agent("Isolated", model=isolated_model), "Separate question", session=isolated
        )
        assert isolated_model.requests[0]["input"] == [
            {"role": "user", "content": "Separate question"}
        ]
        assert _item_order(await isolated.get_items()) == ["user", "msg_isolated"]
        assert await source.get_items() == stored
    finally:
        source.close()
        isolated.close()

    restarted = await _continue_in_new_process(database)
    assert restarted["pid"] != os.getpid()
    assert restarted["output"] == "Retained the previous facts."
    assert restarted["input_order"] == [*_item_order(stored), "user"]
    assert restarted["stored_order"] == [*_item_order(stored), "user", "msg_restart"]


async def test_openai_conversations_session_replays_and_appends_canonical_items() -> None:
    historical_call = call("lookup", call_id="call_saved").model_dump(exclude_unset=True)
    historical_output = {
        "type": "function_call_output",
        "id": "item_saved_output",
        "call_id": "call_saved",
        "output": "fact:Ada",
        "status": "completed",
    }
    historical_reply = {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "Recorded Ada.", "annotations": []}],
    }
    remote_history = [
        {
            "type": "message",
            "id": "msg_saved_user",
            "role": "user",
            "status": "completed",
            "content": [{"type": "input_text", "text": "Remember Ada."}],
        },
        historical_call,
        historical_output,
        {**historical_reply, "id": "msg_saved", "provider_data": {"source": "synthetic"}},
    ]
    original_history = deepcopy(remote_history)
    cursors: list[str | None] = []
    appends: list[dict[str, Any]] = []

    def conversations_api(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/conversations/conv_saved/items"
        if request.method == "POST":
            appends.append(json.loads(request.content))
            return httpx2.Response(200, json={"object": "list", "data": [], "has_more": False})

        assert request.method == "GET"
        assert request.url.params["order"] == "desc"
        cursor = request.url.params.get("after")
        cursors.append(cursor)
        if cursor is None:
            page = remote_history[2:]
        else:
            assert cursor == "item_saved_output"
            page = remote_history[:2]
        return httpx2.Response(
            200, json={"data": page[::-1], "last_id": page[0]["id"], "has_more": cursor is None}
        )

    reasoning = ResponseReasoningItem(
        id="rs_new", summary=[], type="reasoning", encrypted_content="encrypted"
    )
    unstorable = ResponseReasoningItem(id=FAKE_RESPONSES_ID, summary=[], type="reasoning")
    reply = _new_message("I remember Ada.", "msg_new")
    model = ScriptedModel([reasoning, unstorable, reply])
    fresh = {"role": "user", "content": "  Continue.  "}
    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://conversations.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(conversations_api)),
    ) as client:
        session = OpenAIConversationsSession(
            conversation_id="conv_saved",
            openai_client=client,
            session_settings=SessionSettings(limit=3),
        )
        # Pyright treats the store's writable session_id property differently from the protocol.
        result = await SDKRunner.run(
            Agent("Continue", model=model), fresh["content"], session=cast(Session, session)
        )

    expected_input = [historical_call, historical_output, historical_reply, fresh]
    assert cursors == [None, "item_saved_output"]
    assert [request["input"] for request in model.requests] == [expected_input]
    assert result.input == expected_input
    assert result.final_output == "I remember Ada."
    assert result.raw_responses[0].output == [reasoning, unstorable, reply]
    reply_item = reply.model_dump(exclude_unset=True)
    reasoning_item = reasoning.model_dump(exclude_unset=True)
    assert result.to_input_list() == [
        *expected_input,
        reasoning_item,
        unstorable.model_dump(exclude_unset=True),
        reply_item,
    ]
    persisted_reply = reply.model_dump(exclude_unset=True, exclude={"id"})
    assert appends == [{"items": [fresh, reasoning_item, persisted_reply]}]
    assert remote_history == original_history


@pytest.mark.parametrize("wrapped", [False, True], ids=["sqlite", "compaction-wrapper"])
async def test_bounded_classic_sqlite_prunes_only_the_cutoff_output(
    tmp_path: Path, wrapped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = [
        {"role": "user", "content": "Remember Ada and Grace"},
        call("lookup", call_id="call_ada").model_dump(exclude_unset=True),
        {"type": "function_call_output", "call_id": "call_ada", "output": "fact:Ada"},
        call("lookup", call_id="call_grace", arguments='{"value":"Grace"}').model_dump(
            exclude_unset=True
        ),
        {"type": "function_call_output", "call_id": "call_grace", "output": "fact:Grace"},
        _new_message("Recorded Ada and Grace.", "msg_classic").model_dump(exclude_unset=True),
    ]
    fresh: TResponseInputItem = {"role": "user", "content": "Continue"}
    reply = _new_message("I remember Grace.", "msg_native")
    model = ScriptedModel([reply])
    stored = SQLiteSession(
        "bounded", tmp_path / "history.db", session_settings=SessionSettings(limit=4)
    )
    session = (
        OpenAIResponsesCompactionSession(
            stored.session_id,
            stored,
            should_trigger_compaction=lambda _: pytest.fail("SDKRunner evaluated compaction"),
        )
        if wrapped
        else stored
    )
    try:
        await stored.add_items(cast(list[TResponseInputItem], history))
        assert await stored.get_items() == history[2:]
        append = AsyncMock(wraps=stored.add_items)
        monkeypatch.setattr(stored, "add_items", append)

        result = await SDKRunner.run(Agent("Continue", model=model), "Continue", session=session)

        assert result.final_output == "I remember Grace."
        assert [request["input"] for request in model.requests] == [[*history[3:], fresh]]
        assert stored.session_settings == SessionSettings(limit=4)
        delta = [fresh, reply.model_dump(exclude_unset=True)]
        append.assert_awaited_once_with(delta)
        assert await stored.get_items(limit=20) == [*history, *delta]
    finally:
        stored.close()


def test_compaction_wrapper_settings_override_is_local() -> None:
    backing_settings = SessionSettings(limit=1)
    stored = SQLiteSession("bounded", session_settings=backing_settings)
    wrapped = OpenAIResponsesCompactionSession(stored.session_id, stored)
    try:
        override = SessionSettings(limit=0)
        wrapped.session_settings = override
        assert wrapped.session_settings is override
        assert stored.session_settings is backing_settings
        assert backing_settings.limit == 1

        for inherited in (None, SessionSettings()):
            wrapped.session_settings = inherited
            assert wrapped.session_settings is backing_settings
    finally:
        stored.close()


@pytest.mark.parametrize(
    "option", ["state", "storage", "resume", "reconciled_calls", "return_state"]
)
async def test_classic_session_rejects_native_persistence_before_effects(
    option: str, tmp_path: Path
) -> None:
    class UnreadableSession(SQLiteSession):
        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            pytest.fail("conflicting history options read the Classic store")

    class UnreachableProvider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            pytest.fail("conflicting history options resolved the provider")

    profile = tmp_path / "native-profile"
    options: dict[str, Any] = {
        "storage": LocalStorage(profile),
        "resume": "saved-session",
        "reconciled_calls": [
            ReconciledCall(
                thread_id="thread",
                turn_id="turn",
                call_id="call",
                outcome="applied",
                result="confirmed",
            )
        ],
        "return_state": True,
    }
    if option == "state":
        baseline = await SDKRunner.run(
            Agent("Checkpoint", model=ScriptedModel([message("saved")])),
            "Save",
            return_state=True,
        )
        assert baseline.native_result.state is not None
        options["state"] = baseline.native_result.state

    stored = UnreadableSession("conflict")
    try:
        with pytest.raises(ValueError, match="SDKRunner session cannot be combined"):
            await SDKRunner.run(
                Agent("Reject", model="selected"),
                "Continue",
                model_provider=UnreachableProvider(),
                session=stored,
                **{option: options[option]},
            )
        assert not profile.exists()
    finally:
        stored.close()


@pytest.mark.parametrize(
    ("failure", "limit", "wrapped"),
    [
        ("pending", None, False),
        ("orphan", None, False),
        ("duplicate", None, False),
        ("unknown", None, False),
        ("unfinished", None, False),
        ("program", None, False),
        ("reasoning", None, False),
        ("message", None, False),
        ("pending", 1, False),
        ("orphan", 1, False),
        ("unknown", 1, False),
        pytest.param("unknown", 1, True, id="unknown-compaction-wrapper"),
    ],
)
async def test_unsettled_store_fails_before_selected_provider_or_append(
    failure: str, limit: int | None, wrapped: bool
) -> None:
    settings = SessionSettings(limit=limit) if limit is not None else None
    stored = SQLiteSession("held", session_settings=settings)
    session = OpenAIResponsesCompactionSession(stored.session_id, stored) if wrapped else stored
    function = call("lookup", call_id="call_old").model_dump(exclude_none=True)
    output = {"type": "function_call_output", "call_id": "call_old", "output": "fact"}
    history = {
        "pending": [function],
        "orphan": [output],
        "duplicate": [function, output, function, output],
        "unknown": [
            {
                "type": "mcp_approval_request",
                "id": "approval_old",
                "server_label": "external",
                "name": "lookup",
                "arguments": "{}",
            }
        ],
        "unfinished": [{**function, "status": "in_progress"}, output],
        "program": [{**function, "caller": {"type": "program", "caller_id": "pg_old"}}, output],
        "reasoning": [{"type": "reasoning", "id": "rs_missing_summary"}],
        "message": [{"role": "assistant", "content": [{"type": "output_text"}]}],
    }[failure]
    visible = _new_message("Earlier reply.", "msg_earlier").model_dump(exclude_unset=True)
    if limit is not None:
        history.append(visible)

    class UnreachableProvider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            raise AssertionError("selected provider resolved before history admission")

    try:
        await stored.add_items(cast(list[TResponseInputItem], history))
        if limit is not None:
            assert await stored.get_items() == [visible]
        with pytest.raises(UserError, match=r"MIG\.8"):
            await SDKRunner.run(
                Agent("Reject", model="selected"),
                "Continue",
                model_provider=UnreachableProvider(),
                session=session,
            )
        assert await stored.get_items(limit=len(history) + 1) == history
    finally:
        stored.close()


async def test_opaque_compaction_history_fails_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreachableProvider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            pytest.fail("compacted history resolved the selected provider")

    @function_tool
    def lookup(value: str) -> str:
        pytest.fail("compacted history executed a tool")

    monkeypatch.setattr(
        "agents.migration._runner.NativeProvider",
        lambda **_: pytest.fail("compacted history opened a native provider"),
    )
    stored = SQLiteSession("compacted", session_settings=SessionSettings(limit=1))
    session = OpenAIResponsesCompactionSession(stored.session_id, stored)
    visible: TResponseInputItem = {"role": "user", "content": "More context"}
    history: list[TResponseInputItem] = [
        {"type": "compaction", "id": "cmp_old", "encrypted_content": "synthetic"},
        visible,
    ]
    try:
        await stored.add_items(history)
        assert await session.get_items() == [visible]
        append = AsyncMock(wraps=stored.add_items)
        monkeypatch.setattr(stored, "add_items", append)
        with pytest.raises(UserError, match=r"opaque compaction records.*agents\.classic\.Runner"):
            await SDKRunner.run(
                Agent("Reject", model="selected", tools=[lookup]),
                "Continue",
                model_provider=UnreachableProvider(),
                session=session,
            )
        append.assert_not_called()
        assert await stored.get_items(limit=len(history) + 1) == history
    finally:
        stored.close()


async def test_bounded_classic_session_rejects_a_full_read_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agents.migration._session_history._ALL_SESSION_ITEMS_LIMIT", 2)
    stored = SQLiteSession("bounded", session_settings=SessionSettings(limit=1))
    history: list[TResponseInputItem] = [
        {"role": "user", "content": text} for text in ("First", "Second", "Third")
    ]
    model = ScriptedModel()
    try:
        await stored.add_items(history)
        with pytest.raises(UserError, match="snapshot reached the read limit"):
            await SDKRunner.run(Agent("Reject", model=model), "Continue", session=stored)
        assert model.requests == []
        assert await stored.get_items(limit=4) == history
    finally:
        stored.close()


@pytest.mark.parametrize("streamed", [False, True], ids=["run", "run_streamed"])
async def test_cancelled_classic_session_read_is_joined_before_run_exits(streamed: bool) -> None:
    read_started, read_cancelled, release_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    class HeldReadSession:
        session_id = "cancel-read"
        session_settings: SessionSettings | None = None

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            effects.append("read")
            read_started.set()
            try:
                await release_read.wait()
            except asyncio.CancelledError:
                read_cancelled.set()
                await release_read.wait()
            effects.append("read returned")
            return [{"role": "user", "content": "Earlier"}]

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            effects.append("append")

        async def pop_item(self) -> None:
            raise AssertionError("a cancelled read must not pop items")

        async def clear_session(self) -> None:
            raise AssertionError("a cancelled read must not clear history")

    class UnreachableProvider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            effects.append("provider resolved")
            raise AssertionError("a cancelled read must not resolve the provider")

    agent = Agent("Cancel", model="selected")
    store = HeldReadSession()
    provider = UnreachableProvider()
    handle = None
    if streamed:
        handle = SDKRunner.run_streamed(agent, "Continue", model_provider=provider, session=store)
        waiter = asyncio.create_task(handle.get_result())
    else:
        waiter = asyncio.create_task(
            SDKRunner.run(agent, "Continue", model_provider=provider, session=store)
        )

    try:
        await asyncio.wait_for(read_started.wait(), 15)
        if handle is not None:
            handle.cancel()
        else:
            waiter.cancel()
        await asyncio.wait_for(read_cancelled.wait(), 15)
        assert not waiter.done()
        if handle is not None:
            assert not handle.is_complete

        release_read.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, 15)
        assert effects == ["read", "read returned"]
    finally:
        release_read.set()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize(
    ("limit", "wrapped"),
    [
        (None, False),
        (1, False),
        (0, False),
        (-1, False),
        pytest.param(1, True, id="compaction-wrapper"),
    ],
)
async def test_model_cannot_dispatch_a_historical_call_id(limit: int | None, wrapped: bool) -> None:
    effects: list[str] = []

    @function_tool
    def lookup(value: str) -> str:
        effects.append(value)
        return "fact"

    history = [
        {"role": "user", "content": "Remember Ada"},
        call("lookup", call_id="call_old").model_dump(exclude_none=True),
        {"type": "function_call_output", "call_id": "call_old", "output": "fact"},
        _new_message("Recorded Ada.", "msg_classic").model_dump(exclude_none=True),
    ]
    model = ScriptedModel(
        [call("lookup", call_id="call_fresh"), call("lookup", call_id="call_old")],
        [_new_message("Finished.", "msg_native")],
    )
    settings = SessionSettings(limit=limit) if limit is not None else None
    stored = SQLiteSession("replay", session_settings=settings)
    session = OpenAIResponsesCompactionSession(stored.session_id, stored) if wrapped else stored
    try:
        await stored.add_items(cast(list[TResponseInputItem], history))
        with pytest.raises((UserError, ModelBehaviorError), match=r"MIG\.8"):
            await SDKRunner.run(
                Agent("Replay", model=model, tools=[lookup]), "Continue", session=session
            )
        assert len(model.requests) == 1
        visible = history
        if limit is not None:
            visible = history[-1:] if limit == 1 else []
        assert model.requests[0]["input"] == [*visible, {"role": "user", "content": "Continue"}]
        assert effects == []
        assert await stored.get_items(limit=len(history) + 1) == history
    finally:
        stored.close()


async def test_failed_native_execution_leaves_classic_session_history_intact() -> None:
    effects: list[str] = []

    @function_tool
    def lookup(value: str) -> str:
        effects.append(value)
        return "fact"

    original: list[TResponseInputItem] = [{"role": "user", "content": "Earlier"}]
    stored = SQLiteSession("failed")
    try:
        await stored.add_items(original)
        model = ScriptedModel([call("lookup")])
        with pytest.raises(MaxTurnsExceeded):
            await SDKRunner.run(
                Agent("Failed", model=model, tools=[lookup]),
                "Continue",
                max_turns=1,
                session=stored,
            )
        assert effects == ["Ada"]
        assert await stored.get_items() == original
    finally:
        stored.close()


@pytest.mark.parametrize("configured_limit", [None, 2])
async def test_session_append_can_commit_before_raising_and_is_never_retried(
    configured_limit: int | None,
) -> None:
    context = object()
    calls: list[str] = []

    class CommitThenFailSession:
        session_id = "custom"
        session_settings = (
            SessionSettings(limit=configured_limit) if configured_limit is not None else None
        )

        def __init__(self) -> None:
            self.items: list[TResponseInputItem] = [
                {"role": "user", "content": [{"type": "input_text", "text": "Earlier"}]},
                {
                    "type": "reasoning",
                    "id": "rs_earlier",
                    "summary": ({"type": "summary_text", "text": "Earlier context"},),
                },
            ]

        async def get_items(
            self, limit: int | None = None, *, wrapper: RunContextWrapper[Any] | None = None
        ) -> list[TResponseInputItem]:
            assert wrapper is not None and wrapper.context is context
            if configured_limit is None:
                assert limit is None
            else:
                assert limit is not None and limit > configured_limit
            calls.append("read")
            return self.items

        async def add_items(
            self,
            items: list[TResponseInputItem],
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> None:
            assert wrapper is not None and wrapper.context is context
            calls.append("append")
            self.items.extend(items)
            raise OSError("store response lost")

        async def pop_item(self, *, wrapper: RunContextWrapper[Any] | None = None) -> None:
            raise AssertionError("append must not be rolled back")

        async def clear_session(self, *, wrapper: RunContextWrapper[Any] | None = None) -> None:
            raise AssertionError("append must not be rolled back")

    class MutatingModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            kwargs["input"][0]["content"][0]["text"] = "Model-local edit"
            return await super().get_response(*args, **kwargs)

    store = CommitThenFailSession()
    model = MutatingModel([_new_message("Completed", "msg_committed")])
    handle = SDKRunner.run_streamed(
        Agent("Append", model=model), "New question", context=context, session=store
    )
    with pytest.raises(OSError, match="store response lost"):
        await anext(handle.stream_events())
    with pytest.raises(OSError, match="store response lost"):
        await handle.get_result()
    assert calls == ["read", "append"]
    assert _item_order(store.items) == ["user", "rs_earlier", "user", "msg_committed"]
    assert store.items[0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "Earlier"}],
    }
    assert model.requests[0]["input"][1]["summary"] == [
        {"type": "summary_text", "text": "Earlier context"}
    ]


@pytest.mark.parametrize("cancel", [False, True], ids=["success", "cancel"])
async def test_stream_waits_for_classic_session_append(cancel: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    writes: list[list[TResponseInputItem]] = []

    class HeldAppendSession(SQLiteSession):
        async def add_items(self, items: list[TResponseInputItem]) -> None:
            writes.append(items)
            entered.set()
            await release.wait()
            await super().add_items(items)

    stored = HeldAppendSession("stream")
    reply = _new_message("Completed", "msg_streamed")
    agent = Agent("Stream", model=ScriptedModel([reply]))
    handle = SDKRunner.run_streamed(agent, "Continue", session=stored)
    reader = asyncio.ensure_future(anext(handle.stream_events()))
    waiter = asyncio.create_task(handle.get_result())
    try:
        await asyncio.wait_for(entered.wait(), 15)
        assert not handle.is_complete
        assert handle.final_output is None
        assert not reader.done()
        assert not waiter.done()
        assert await stored.get_items() == []
        if cancel:
            handle.cancel()
            await asyncio.sleep(0)
            assert not handle.is_complete

        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reader, 15)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 15)
        else:
            assert await asyncio.wait_for(reader, 15) == AgentUpdatedStreamEvent(new_agent=agent)
            result = await asyncio.wait_for(waiter, 15)
            assert result.final_output == "Completed"
        assert handle.is_complete
        expected = [{"role": "user", "content": "Continue"}, reply.model_dump(exclude_unset=True)]
        assert writes == [expected]
        assert await stored.get_items() == expected
    finally:
        handle.cancel()
        release.set()
        await asyncio.gather(reader, waiter, return_exceptions=True)
        stored.close()


if __name__ == "__main__":
    asyncio.run(_restart(sys.argv[1]))
