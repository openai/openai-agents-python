"""Compaction is a billed model call, so its tokens must not be discarded."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.memory.openai_responses_compaction_session import (
    OpenAIResponsesCompactionSession,
)
from agents.memory.session import _call_session_method, _session_method_accepts_wrapper
from agents.run_context import RunContextWrapper
from agents.memory.session import Session


def _compact_response(input_tokens: int, output_tokens: int) -> MagicMock:
    response = MagicMock()
    response.output = []
    response.usage = MagicMock()
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.usage.total_tokens = input_tokens + output_tokens
    response.usage.input_tokens_details = InputTokensDetails(cached_tokens=0, cache_write_tokens=0)
    response.usage.output_tokens_details = OutputTokensDetails(reasoning_tokens=0)
    return response


def _session(client: MagicMock) -> OpenAIResponsesCompactionSession:
    return OpenAIResponsesCompactionSession(
        session_id="s1",
        underlying_session=MagicMock(spec=Session),
        client=client,
        model="gpt-4.1",
        should_trigger_compaction=lambda _args: True,
    )


def test_compaction_usage_starts_empty() -> None:
    session = _session(MagicMock())
    assert session.compaction_usage.total_tokens == 0
    assert session.compaction_usage.requests == 0


@pytest.mark.asyncio
async def test_compaction_usage_is_recorded() -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=_compact_response(150_000, 42_000))
    session = _session(client)
    session._response_id = "resp_1"
    session._session_items = []
    session._compaction_candidate_items = []

    await session.run_compaction()

    assert session.compaction_usage.requests == 1
    assert session.compaction_usage.input_tokens == 150_000
    assert session.compaction_usage.output_tokens == 42_000
    assert session.compaction_usage.total_tokens == 192_000


@pytest.mark.asyncio
async def test_compaction_usage_accumulates_across_passes() -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(
        side_effect=[_compact_response(100, 10), _compact_response(200, 20)]
    )
    session = _session(client)
    session._response_id = "resp_1"
    session._session_items = []
    session._compaction_candidate_items = []

    await session.run_compaction()
    await session.run_compaction()

    assert session.compaction_usage.requests == 2
    assert session.compaction_usage.total_tokens == 330


@pytest.mark.asyncio
async def test_missing_usage_is_tolerated() -> None:
    response = MagicMock()
    response.output = []
    response.usage = None
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=response)
    session = _session(client)
    session._response_id = "resp_1"
    session._session_items = []
    session._compaction_candidate_items = []

    await session.run_compaction()

    assert session.compaction_usage.total_tokens == 0


@pytest.mark.asyncio
async def test_compaction_usage_reaches_the_run_context() -> None:
    """The run's reported total must include compaction, not just the session."""
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=_compact_response(150_000, 42_000))
    session = _session(client)
    session._response_id = "resp_1"
    session._session_items = []
    session._compaction_candidate_items = []

    wrapper: RunContextWrapper[None] = RunContextWrapper(context=None)
    await session.run_compaction(wrapper=wrapper)

    assert wrapper.usage.total_tokens == 192_000
    assert wrapper.usage.requests == 1


def test_run_compaction_opts_into_the_wrapper_protocol() -> None:
    """The run loop only passes wrapper to methods that declare it."""
    session = _session(MagicMock())
    assert _session_method_accepts_wrapper(session.run_compaction)


@pytest.mark.asyncio
async def test_run_loop_call_shape_passes_wrapper() -> None:
    """Exercise the helper the run loop actually uses."""
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=_compact_response(100, 10))
    session = _session(client)
    session._response_id = "resp_1"
    session._session_items = []
    session._compaction_candidate_items = []

    wrapper: RunContextWrapper[None] = RunContextWrapper(context=None)
    await _call_session_method(session.run_compaction, None, wrapper=wrapper)

    assert wrapper.usage.total_tokens == 110
