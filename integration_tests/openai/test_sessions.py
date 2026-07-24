from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    ModelSettings,
    OpenAIConversationsSession,
    OpenAIResponsesCompactionSession,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    SQLiteSession,
)
from agents.decorators import tool

pytestmark = pytest.mark.core


async def test_sqlite_session_persists_tool_history_across_reopened_instances(
    integration_model: str, tmp_path: Path
) -> None:
    calls: list[str] = []

    @tool
    def lookup_codeword(label: str) -> str:
        """Look up the deterministic secret codeword."""
        calls.append(label)
        return "MARIGOLD"

    agent = Agent(
        name="Packaged SQLite session agent",
        model=integration_model,
        instructions="Use lookup_codeword when requested and remember its result.",
        model_settings={"max_tokens": 384},
        tools=[lookup_codeword],
    )
    database = tmp_path / "conversation.sqlite3"
    session = SQLiteSession("packaged-live-session", database)
    config = RunConfig(tracing_disabled=True)
    try:
        first = await Runner.run(
            agent,
            "Use lookup_codeword with label='release' and reply only STORED.",
            session=session,
            run_config=config,
        )
        assert first.final_output == "STORED"
        assert calls == ["release"]
    finally:
        session.close()

    reopened = SQLiteSession("packaged-live-session", database)
    try:
        second = await Runner.run(
            agent,
            "What exact codeword did the tool return? Answer with that word only.",
            session=reopened,
            run_config=config,
        )
        saved_items = await reopened.get_items()
    finally:
        reopened.close()

    assert second.final_output.strip().upper() == "MARIGOLD"
    assert calls == ["release"]
    assert any(item.get("type") == "function_call_output" for item in saved_items)


async def test_explicit_input_replay_preserves_a_real_response_history(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged explicit replay agent",
        model=integration_model,
        model_settings={"max_tokens": 256},
    )
    config = RunConfig(tracing_disabled=True)
    first = await Runner.run(
        agent,
        "Remember that the verification number is 907. Reply only STORED.",
        run_config=config,
    )
    replay = first.to_input_list()
    assert any(
        item.get("role") == "user" and "verification number is 907" in str(item.get("content"))
        for item in replay
    )
    second = await Runner.run(
        agent,
        replay
        + [
            {
                "role": "user",
                "content": "What verification number did I provide? Reply with only the number.",
            }
        ],
        run_config=config,
    )

    assert second.final_output.strip() == "907"


async def test_streamed_previous_response_id_continues_server_managed_history(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged streamed continuation agent",
        model=integration_model,
        model_settings={"max_tokens": 256},
    )
    config = RunConfig(tracing_disabled=True)
    first = await Runner.run(
        agent,
        "Remember that the state token is IVORY. Reply only STORED.",
        run_config=config,
    )
    assert first.last_response_id is not None

    second = Runner.run_streamed(
        agent,
        "What state token did I provide? Answer with only the token.",
        previous_response_id=first.last_response_id,
        run_config=config,
    )
    async for _event in second.stream_events():
        pass

    assert second.final_output.strip().upper() == "IVORY"
    assert second.last_response_id != first.last_response_id


async def test_openai_conversation_id_preserves_server_owned_state(
    integration_model: str,
) -> None:
    client = AsyncOpenAI()
    conversation = await client.conversations.create()
    agent = Agent(
        name="Packaged OpenAI conversation agent",
        model=integration_model,
        model_settings={"max_tokens": 256},
    )
    config = RunConfig(tracing_disabled=True)

    try:
        await Runner.run(
            agent,
            "Remember the project color is CERULEAN. Reply only STORED.",
            conversation_id=conversation.id,
            run_config=config,
        )
        second = await Runner.run(
            agent,
            "What is the project color? Reply with only the color.",
            conversation_id=conversation.id,
            run_config=config,
        )
    finally:
        await client.conversations.delete(conversation.id)

    assert second.final_output.strip().upper() == "CERULEAN"


async def test_auto_previous_response_id_preserves_tool_output_across_turns(
    integration_model: str,
) -> None:
    calls: list[str] = []

    @tool
    def read_checkpoint(name: str) -> str:
        """Read a deterministic server-managed continuation checkpoint."""
        calls.append(name)
        return "CHECKPOINT:84"

    agent = Agent(
        name="Packaged automatic continuation agent",
        model=integration_model,
        instructions=(
            "Call read_checkpoint with name='release', then reply exactly AUTO_CONTINUATION:84."
        ),
        model_settings={"max_tokens": 384},
        tools=[read_checkpoint],
    )
    result = await Runner.run(
        agent,
        "Read the release checkpoint.",
        auto_previous_response_id=True,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert calls == ["release"]
    assert result.final_output == "AUTO_CONTINUATION:84"
    assert result.last_response_id is not None


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_openai_conversations_session_preserves_server_managed_history(
    integration_model: str,
    streaming: bool,
) -> None:
    session = OpenAIConversationsSession(session_settings={"limit": 20})
    agent = Agent(
        name="Packaged OpenAI Conversations session agent",
        model=integration_model,
        instructions="Remember user-provided release words and follow exact output instructions.",
        model_settings={"max_tokens": 192},
    )
    config = RunConfig(tracing_disabled=True)

    try:
        first = await Runner.run(
            agent,
            "Remember the release word COBALT and reply only STORED.",
            session=session,
            run_config=config,
        )
        second: RunResult | RunResultStreaming
        if streaming:
            second = Runner.run_streamed(
                agent,
                "What release word did I give you? Reply with that word only.",
                session=session,
                run_config=config,
            )
            async for _event in second.stream_events():
                pass
        else:
            second = await Runner.run(
                agent,
                "What release word did I give you? Reply with that word only.",
                session=session,
                run_config=config,
            )
        items = await session.get_items()
    finally:
        await session.clear_session()

    assert first.final_output == "STORED"
    assert second.final_output == "COBALT"
    assert len(items) >= 4
    assert any("COBALT" in str(item) for item in items)


@pytest.mark.nightly
@pytest.mark.parametrize(
    ("compaction_mode", "store"),
    [("auto", False), ("previous_response_id", True)],
    ids=["stateless-input", "stored-previous-response"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_responses_compaction_preserves_history_across_owner_modes(
    integration_model: str,
    compaction_mode: Literal["auto", "previous_response_id"],
    store: bool,
    streaming: bool,
) -> None:
    underlying = SQLiteSession(f"packaged-compaction-{compaction_mode}-{streaming}")
    compacted = OpenAIResponsesCompactionSession(
        session_id=underlying.session_id,
        underlying_session=underlying,
        model=integration_model,
        compaction_mode=compaction_mode,
        should_trigger_compaction=lambda context: bool(context["compaction_candidate_items"]),
    )
    agent = Agent(
        name="Packaged Responses compaction agent",
        model=integration_model,
        instructions="Remember user-provided release words and follow exact output instructions.",
        model_settings=ModelSettings(store=store, max_tokens=192),
    )
    config = RunConfig(tracing_disabled=True)

    try:
        first = await Runner.run(
            agent,
            "Remember the release word JASPER and reply only STORED.",
            session=compacted,
            run_config=config,
        )
        first_items = await underlying.get_items()
        second: RunResult | RunResultStreaming
        if streaming:
            second = Runner.run_streamed(
                agent,
                "What release word did I give you? Reply with that word only.",
                session=compacted,
                run_config=config,
            )
            async for _event in second.stream_events():
                pass
        else:
            second = await Runner.run(
                agent,
                "What release word did I give you? Reply with that word only.",
                session=compacted,
                run_config=config,
            )
        second_items = await underlying.get_items()
    finally:
        underlying.close()

    assert first.final_output == "STORED"
    assert second.final_output == "JASPER"
    assert any(item.get("type") == "compaction" for item in first_items)
    assert any(item.get("type") == "compaction" for item in second_items)
