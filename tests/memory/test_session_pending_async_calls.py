"""Imported async API calls must survive the next committed-session read."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents import Agent, ModelSettings, RunConfig, Runner, SQLiteSession, TResponseInputItem
from agents.memory import OpenAIResponsesCompactionSession
from agents.retry import ModelRetryAdvice, ModelRetrySettings, retry_policies
from agents.run_internal.items import normalize_resumed_input, prepare_model_input_items
from agents.run_internal.session_persistence import _held_items_safe_to_settle
from agents.testing import ModelStep, ScriptedModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


def imported_history(call_type: str = "function_call") -> list[TResponseInputItem]:
    # These opaque placeholders test exact forwarding, not server-side prefix validation.
    return cast(
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
                "status": "completed",
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


async def run(
    agent: Agent[Any], session: Any, *, streamed: bool = False, config: RunConfig | None = None
) -> None:
    config = config or RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, "Continue.", session=session, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, "Continue.", session=session, run_config=config)


@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("streamed", [False, True])
async def test_pending_async_history_and_later_output_reach_runner_request(
    call_type: str, streamed: bool
) -> None:
    history = imported_history(call_type)
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
        pytest.param("status", None, id="null-status-outside-scope"),
        pytest.param("status", "omit", id="missing-status-outside-scope"),
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
async def test_generated_resume_and_held_write_cleanup_remain_strict(call_type: str) -> None:
    history = imported_history(call_type)
    expected = [history[0], *history[3:]]
    # These cleanup boundaries own incomplete SDK work, not committed imported history.
    assert prepare_model_input_items([], history) == expected
    assert normalize_resumed_input(history) == expected
    assert _held_items_safe_to_settle(history, [], None) == expected
