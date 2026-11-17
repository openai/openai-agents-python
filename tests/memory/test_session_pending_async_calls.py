"""Imported async API calls must survive the next committed-session read."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.responses import ResponseCustomToolCall

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    SQLiteSession,
    TResponseInputItem,
)
from agents.decorators import tool
from agents.memory import OpenAIResponsesCompactionSession
from agents.models.openai_responses import OpenAIResponsesModel
from agents.retry import ModelRetryAdvice, ModelRetrySettings, retry_policies
from agents.run_internal.items import normalize_resumed_input, prepare_model_input_items
from agents.run_internal.session_persistence import _held_items_safe_to_settle
from agents.testing import ModelStep, ScriptedModel
from tests.model_test_helpers import get_response_obj
from tests.test_responses import get_function_tool_call, get_text_message

pytestmark = pytest.mark.asyncio


def imported_history(
    call_type: str = "function_call", *, status: str | None = "completed"
) -> list[TResponseInputItem]:
    # These opaque placeholders test exact forwarding, not server-side prefix validation.
    history = cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "Start the external job."},
            {
                "type": "reasoning",
                "id": "rs_before",
                "summary": [],
                "encrypted_content": "synthetic-before",
            },
            {
                "type": call_type,
                "id": "tool_external",
                "call_id": "call_external",
                "name": "external_job",
                "arguments" if call_type == "function_call" else "input": "{}",
                "async": True,
                "status": status,
            },
            {
                "type": "reasoning",
                "id": "rs_after",
                "summary": [],
                "encrypted_content": "synthetic-after",
            },
            {"role": "assistant", "content": "The external job is pending."},
        ],
    )
    if status == "omitted":
        cast(dict[str, Any], history[2]).pop("status")
    return history


async def run(
    agent: Agent[Any],
    session: Any,
    *,
    run_input: str | RunState[Any] = "Continue.",
    streamed: bool = False,
    config: RunConfig | None = None,
) -> RunResult | RunResultStreaming:
    config = config or RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, run_input, session=session, run_config=config)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, run_input, session=session, run_config=config)


@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "status", ["completed", "omitted", None], ids=["completed", "omitted", "null"]
)
async def test_pending_async_history_and_later_output_reach_runner_request(
    call_type: str, streamed: bool, status: str | None
) -> None:
    history = imported_history(call_type, status=status)
    session = SQLiteSession("pending")
    model = ScriptedModel([[get_text_message("Still pending.")], [get_text_message("Done.")]])
    agent = Agent(name="test", model=model)
    try:
        await session.add_items(history)
        await run(agent, session, streamed=streamed)
        assert model.calls[0].input == [*history, {"role": "user", "content": "Continue."}]
        assert (await session.get_items())[: len(history)] == history

        output = cast(
            TResponseInputItem,
            {"type": f"{call_type}_output", "call_id": "call_external", "output": "job result"},
        )
        await session.add_items([output])
        committed = await session.get_items()
        await run(agent, session, streamed=streamed)
        assert model.calls[1].input == [*committed, {"role": "user", "content": "Continue."}]
        assert (await session.get_items())[: len(committed)] == committed
    finally:
        session.close()


@pytest.mark.parametrize("streamed", [False, True])
async def test_serialized_async_custom_call_survives_session_replay(streamed: bool) -> None:
    call = ResponseCustomToolCall.model_validate(
        {
            "type": "custom_tool_call",
            "id": "tool_external",
            "call_id": "call_external",
            "name": "external_job",
            "input": "{}",
            "async": True,
        }
    ).model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert "status" not in call
    assert call["async"] is True
    history = imported_history("custom_tool_call", status="omitted")
    history[2] = cast(TResponseInputItem, call)
    session = SQLiteSession("serialized-custom-call")
    model = ScriptedModel([[get_text_message("Still pending.")]])
    try:
        await session.add_items(history)
        assert await session.get_items() == history
        await run(Agent(name="test", model=model), session, streamed=streamed)
        assert model.calls[0].input == [*history, {"role": "user", "content": "Continue."}]
        assert (await session.get_items())[: len(history)] == history
    finally:
        session.close()


@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialized", [False, True])
async def test_pending_async_history_survives_unrelated_approval_resume(
    call_type: str, streamed: bool, serialized: bool
) -> None:
    history = imported_history(call_type, status="omitted")[:4]
    if call_type == "custom_tool_call":
        history[2] = cast(
            TResponseInputItem,
            ResponseCustomToolCall.model_validate(history[2]).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            ),
        )
    executions: list[str] = []

    @tool(needs_approval=True)
    def unrelated_tool() -> str:
        executions.append("approved")
        return "approved result"

    model = ScriptedModel(
        [
            [get_function_tool_call("unrelated_tool", "{}", call_id="call_approval")],
            [get_text_message("Done.")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[unrelated_tool])
    session = SQLiteSession("pending-approval")
    expected_input = [*history, {"role": "user", "content": "Continue."}]
    try:
        await session.add_items(history)
        first = await run(agent, session, streamed=streamed)
        assert model.calls[0].input == expected_input
        assert len(first.interruptions) == 1
        assert executions == []
        state = first.to_state()
        if serialized:
            state = await RunState.from_string(agent, state.to_string())
        assert state._original_input == expected_input
        state.approve(state.get_interruptions()[0])

        resumed = await run(agent, session, run_input=state, streamed=streamed)

        assert resumed.final_output == "Done."
        assert executions == ["approved"]
        assert len(model.calls) == 2
        resumed_input = model.calls[1].input
        assert isinstance(resumed_input, list)
        assert resumed_input[: len(expected_input)] == expected_input
        assert [item.get("type") for item in resumed_input[len(expected_input) :]] == [
            "function_call",
            "function_call_output",
        ]
        committed = await session.get_items()
        assert committed[: len(expected_input)] == expected_input
        assert committed.count(history[2]) == 1
    finally:
        session.close()


@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize(
    "status", ["completed", "omitted", None], ids=["completed", "omitted", "null"]
)
async def test_pending_async_status_reaches_responses_wire_without_rewriting(
    call_type: str, status: str | None
) -> None:
    history = imported_history(call_type, status=status)
    requests: list[dict[str, Any]] = []

    # A real provider adapter and HTTP serialization are needed to distinguish null from omission.
    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=get_response_obj([get_text_message("ok")]).model_dump())

    session = SQLiteSession("wire")
    try:
        await session.add_items(history)
        assert await session.get_items() == history
        async with AsyncOpenAI(
            api_key="test-key",
            base_url="https://provider.example/v1",
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(respond), trust_env=False
            ),
        ) as client:
            agent = Agent(
                name="test", model=OpenAIResponsesModel(model="fake", openai_client=client)
            )
            await run(agent, session)
        assert requests[0]["input"] == [*history, {"role": "user", "content": "Continue."}]
        call = requests[0]["input"][2]
        assert ("status" in call) is (status != "omitted")
        if status != "omitted":
            assert call["status"] == status
    finally:
        session.close()


@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("async", False, id="sync"),
        pytest.param("async", None, id="null-async"),
        pytest.param("async", "true", id="string-async"),
        pytest.param("async", 1, id="numeric-async"),
        pytest.param("async", "omit", id="missing-async"),
        pytest.param("call_id", "", id="empty-call-id"),
        pytest.param("call_id", None, id="null-call-id"),
        pytest.param("status", "incomplete", id="incomplete-emission"),
        pytest.param("status", "in_progress", id="emission-in-progress"),
    ],
)
async def test_session_still_prunes_sync_and_unsupported_orphans(
    call_type: str, field: str, value: Any
) -> None:
    history = imported_history(call_type)
    call = cast(dict[str, Any], history[2])
    if value == "omit":
        call.pop(field)
    else:
        call[field] = value
    session = SQLiteSession("orphan")
    model = ScriptedModel([[get_text_message("ok")]])
    try:
        await session.add_items(history)
        await run(Agent(name="test", model=model), session)
        assert model.calls[0].input == [
            history[0],
            *history[3:],
            {"role": "user", "content": "Continue."},
        ]
        assert (await session.get_items())[: len(history)] == history
        assert normalize_resumed_input(history) == [history[0], *history[3:]]
    finally:
        session.close()


@pytest.mark.parametrize("edit", ["remove", "redact", "make-sync"])
async def test_session_callback_edits_are_not_restored(edit: str) -> None:
    history = imported_history()
    expected = deepcopy(history)
    if edit == "remove":
        del expected[1:3]
    elif edit == "redact":
        cast(dict[str, Any], expected[2])["arguments"] = '{"redacted":true}'
    else:
        cast(dict[str, Any], expected[2])["async"] = False

    def edit_history(
        items: list[TResponseInputItem], new: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        if edit == "remove":
            del items[1:3]
        elif edit == "redact":
            cast(dict[str, Any], items[2])["arguments"] = '{"redacted":true}'
        else:
            cast(dict[str, Any], items[2])["async"] = False
        return items + new

    if edit == "make-sync":
        del expected[1:3]
    session = SQLiteSession("edited")
    model = ScriptedModel([[get_text_message("ok")]])
    try:
        await session.add_items(history)
        await run(
            Agent(name="test", model=model),
            session,
            config=RunConfig(session_input_callback=edit_history, tracing_disabled=True),
        )
        assert model.calls[0].input == [*expected, {"role": "user", "content": "Continue."}]
        assert (await session.get_items())[: len(history)] == history
    finally:
        session.close()


async def test_pending_history_survives_model_retry_without_duplicate_persistence() -> None:
    history = imported_history()
    session = SQLiteSession("retry")
    model = ScriptedModel(
        [
            ModelStep.raise_error(
                RuntimeError("synthetic transient failure"),
                retry_advice=ModelRetryAdvice(suggested=True, replay_safety="safe"),
            ),
            [get_text_message("ok")],
        ]
    )
    try:
        await session.add_items(history)
        await run(
            Agent(name="test", model=model),
            session,
            config=RunConfig(
                tracing_disabled=True,
                model_settings=ModelSettings(
                    retry=ModelRetrySettings(
                        max_retries=1,
                        backoff={"initial_delay": 0},
                        policy=retry_policies.provider_suggested(),
                    )
                ),
            ),
        )
        expected = [*history, {"role": "user", "content": "Continue."}]
        assert [call.input for call in model.calls] == [expected, expected]
        assert (await session.get_items())[:-1] == expected
    finally:
        session.close()


@pytest.mark.parametrize("replacement", ["clear", "compaction"])
async def test_replaced_session_history_is_not_resurrected(replacement: str) -> None:
    underlying = SQLiteSession("replace")
    client = MagicMock()
    summary = cast(
        TResponseInputItem,
        {"type": "compaction", "id": "cmp_test", "encrypted_content": "synthetic-summary"},
    )
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "replace",
        underlying,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: False,
    )
    model = ScriptedModel([[get_text_message("ok")]])
    try:
        await session.add_items(imported_history())
        if replacement == "clear":
            await session.clear_session()
            expected = []
        else:
            await session.run_compaction({"force": True})
            expected = [summary]
        assert await session.get_items() == expected
        await run(Agent(name="test", model=model), session)
        assert model.calls[0].input == [*expected, {"role": "user", "content": "Continue."}]
    finally:
        underlying.close()


@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize(
    "status", ["completed", "omitted", None], ids=["completed", "omitted", "null"]
)
async def test_generated_and_held_write_cleanup_remain_strict(
    call_type: str, status: str | None
) -> None:
    history = imported_history(call_type, status=status)
    expected = [history[0], *history[3:]]
    # These cleanup boundaries own incomplete SDK work, not committed imported history.
    assert prepare_model_input_items([], history) == expected
    assert _held_items_safe_to_settle(history, [], None) == expected
