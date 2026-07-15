"""Regression tests for ``get_trace_tool_error`` redaction at the four
``SpanError`` call sites that previously embedded ``str(e)`` directly.

The four sites covered here are:

* ``run_internal/run_loop.py`` — the inner and outer exception handlers in
  ``start_streaming`` (the streamed-run loop).
* ``run_internal/turn_preparation.py`` — the exception handler in
  ``maybe_filter_model_input``.
* ``voice/models/openai_stt.py`` — the exception handler in
  ``OpenAISTTModel.transcribe``.

For each site we assert that when ``trace_include_sensitive_data`` is ``False``
the exported span payload carries the redaction constant from
``get_trace_tool_error`` (and never the raw exception message), and that when
the flag is ``True`` the original exception message is preserved.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from agents import (
    Agent,
    AgentsException,
    RunConfig,
    Runner,
    custom_span,
    trace,
)
from agents.run_context import RunContextWrapper
from agents.run_internal.turn_preparation import maybe_filter_model_input
from agents.util._tool_errors import REDACTED_TOOL_ERROR_MESSAGE, get_trace_tool_error
from agents.voice.input import AudioInput
from agents.voice.model import STTModelSettings
from agents.voice.models.openai_stt import OpenAISTTModel

from .testing_processor import SPAN_PROCESSOR_TESTING

PII_MARKER = "user-PII-secret-12345"


def _exported_spans() -> list[dict[str, Any]]:
    """Return the raw exported span payloads recorded by the test processor."""
    spans: list[dict[str, Any]] = []
    for span in SPAN_PROCESSOR_TESTING.get_ordered_spans(including_empty=True):
        exported = span.export()
        if not exported:
            continue
        spans.append(exported)
    return spans


def _spans_of_type(span_type: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for exported in _exported_spans():
        span_data = exported.get("span_data")
        if not isinstance(span_data, dict):
            continue
        if span_data.get("type") != span_type:
            continue
        matched.append(exported)
    return matched


def _agent_span_errors() -> list[dict[str, Any] | None]:
    return [exported.get("error") for exported in _spans_of_type("agent")]


def _transcription_span_errors() -> list[dict[str, Any] | None]:
    return [exported.get("error") for exported in _spans_of_type("transcription")]


# ---------------------------------------------------------------------------
# Sites 1 & 2: run_internal/run_loop.py start_streaming exception handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streamed_agent_error_redacted_when_trace_include_sensitive_data_false() -> None:
    """When the flag is False, the agent span error must not carry the raw exception text.

    FakeModel raises ``RuntimeError(PII_MARKER)``. The exception is caught by
    the inner exception handler in ``start_streaming`` (attaches a SpanError),
    re-raised, then caught by the outer exception handler (overwrites the
    SpanError). Both handlers route through ``get_trace_tool_error`` after this
    fix, so the final agent span error must carry the redaction constant.
    """
    from .fake_model import FakeModel

    model = FakeModel(tracing_enabled=True)
    model.set_next_output(RuntimeError(PII_MARKER))

    agent = Agent(name="redact_test", model=model)

    with pytest.raises(RuntimeError):
        result = Runner.run_streamed(
            agent,
            input="hello",
            run_config=RunConfig(trace_include_sensitive_data=False),
        )
        async for _ in result.stream_events():
            pass

    agent_errors = [err for err in _agent_span_errors() if err is not None]
    assert agent_errors, "expected at least one agent span error"
    for error in agent_errors:
        assert error["message"] == "Error in agent run"
        assert error["data"]["error"] == REDACTED_TOOL_ERROR_MESSAGE
        assert PII_MARKER not in str(error)


@pytest.mark.asyncio
async def test_run_streamed_agent_error_kept_when_trace_include_sensitive_data_true() -> None:
    """When the flag is True (default), the agent span error must carry the raw exception text."""
    from .fake_model import FakeModel

    model = FakeModel(tracing_enabled=True)
    model.set_next_output(RuntimeError(PII_MARKER))

    agent = Agent(name="keep_test", model=model)

    with pytest.raises(RuntimeError):
        result = Runner.run_streamed(
            agent,
            input="hello",
            run_config=RunConfig(trace_include_sensitive_data=True),
        )
        async for _ in result.stream_events():
            pass

    agent_errors = [err for err in _agent_span_errors() if err is not None]
    assert agent_errors, "expected at least one agent span error"
    for error in agent_errors:
        assert error["message"] == "Error in agent run"
        assert error["data"]["error"] == PII_MARKER


class _PIIAgentsException(AgentsException):
    """A non-filtered ``AgentsException`` subclass used to exercise the inner
    handler in ``start_streaming`` in isolation.

    Because ``AgentsException`` subclasses are caught by the
    ``except AgentsException`` clause (which does not attach a second
    SpanError), the only SpanError attached to the agent span is the one
    attached by the inner handler at ``run_loop.py`` site 1.
    """


@pytest.mark.asyncio
async def test_run_streamed_inner_handler_redacts_agents_exception_when_flag_false() -> None:
    """Inner-handler site (``run_loop.py`` site 1) is exercised in isolation.

    A non-filtered ``AgentsException`` raised by the model is caught by the
    inner handler (which attaches a SpanError via ``get_trace_tool_error``)
    and then re-raised. The outer ``except AgentsException`` clause catches
    the re-raised exception and does NOT attach a second SpanError, so the
    agent span error reflects the inner handler's redacted form only.
    """
    from .fake_model import FakeModel

    model = FakeModel(tracing_enabled=True)
    model.set_next_output(_PIIAgentsException(PII_MARKER))

    agent = Agent(name="inner_handler_test", model=model)

    with pytest.raises(_PIIAgentsException):
        result = Runner.run_streamed(
            agent,
            input="hello",
            run_config=RunConfig(trace_include_sensitive_data=False),
        )
        async for _ in result.stream_events():
            pass

    agent_errors = [err for err in _agent_span_errors() if err is not None]
    assert agent_errors, "expected at least one agent span error"
    for error in agent_errors:
        assert error["message"] == "Error in agent run"
        assert error["data"]["error"] == REDACTED_TOOL_ERROR_MESSAGE
        assert PII_MARKER not in str(error)


# ---------------------------------------------------------------------------
# Site 3: run_internal/turn_preparation.py maybe_filter_model_input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_filter_model_input_error_redacted_when_trace_include_sensitive_data_false(  # noqa: E501
) -> None:
    """When the flag is False, the SpanError attached by ``maybe_filter_model_input``
    must carry the redaction constant, not the raw exception message."""

    def raising_filter(_: Any) -> Any:
        raise ValueError(PII_MARKER)

    agent = Agent[Any](name="filter_test")
    context_wrapper = RunContextWrapper(None)

    with trace("filter_redact_test"):
        with custom_span(name="filter_test_span"):
            with pytest.raises(ValueError):
                await maybe_filter_model_input(
                    agent=agent,
                    run_config=RunConfig(
                        trace_include_sensitive_data=False,
                        call_model_input_filter=raising_filter,
                    ),
                    context_wrapper=context_wrapper,
                    input_items=[{"role": "user", "content": "hi"}],
                    system_instructions=None,
                )

    spans = _exported_spans()
    assert spans, "expected at least one span to be recorded"
    # The current span at the point of the exception is the custom span above.
    # We assert that the span whose ``error`` is set carries the redaction
    # constant in ``data["error"]`` and never the PII marker.
    found_redacted_error = False
    for exported in spans:
        error = exported.get("error")
        if error is None:
            continue
        assert error["message"] == "Error in call_model_input_filter"
        assert error["data"]["error"] == REDACTED_TOOL_ERROR_MESSAGE
        assert PII_MARKER not in str(error)
        found_redacted_error = True
    assert found_redacted_error, "expected at least one span with the call_model_input_filter error"


@pytest.mark.asyncio
async def test_maybe_filter_model_input_error_kept_when_trace_include_sensitive_data_true() -> None:
    """When the flag is True, the SpanError attached by ``maybe_filter_model_input``
    must carry the raw exception message."""

    def raising_filter(_: Any) -> Any:
        raise ValueError(PII_MARKER)

    agent = Agent[Any](name="filter_keep_test")
    context_wrapper = RunContextWrapper(None)

    with trace("filter_keep_test"):
        with custom_span(name="filter_test_span"):
            with pytest.raises(ValueError):
                await maybe_filter_model_input(
                    agent=agent,
                    run_config=RunConfig(
                        trace_include_sensitive_data=True,
                        call_model_input_filter=raising_filter,
                    ),
                    context_wrapper=context_wrapper,
                    input_items=[{"role": "user", "content": "hi"}],
                    system_instructions=None,
                )

    spans = _exported_spans()
    found_kept_error = False
    for exported in spans:
        error = exported.get("error")
        if error is None:
            continue
        assert error["message"] == "Error in call_model_input_filter"
        assert error["data"]["error"] == PII_MARKER
        found_kept_error = True
    assert found_kept_error, "expected at least one span with the call_model_input_filter error"


# ---------------------------------------------------------------------------
# Site 4: voice/models/openai_stt.py OpenAISTTModel.transcribe
# ---------------------------------------------------------------------------


def _build_stt_model_with_raising_client() -> OpenAISTTModel:
    """Construct an ``OpenAISTTModel`` whose OpenAI audio client raises on
    ``transcriptions.create`` with a PII-bearing message."""
    mock_client = AsyncMock()
    mock_client.audio.transcriptions.create = AsyncMock(side_effect=RuntimeError(PII_MARKER))
    return OpenAISTTModel(model="whisper-1", openai_client=mock_client)


@pytest.mark.asyncio
async def test_stt_transcribe_error_redacted_when_trace_include_sensitive_data_false() -> None:
    """When the flag is False, the transcription span error message must be
    the redaction constant, not the raw OpenAI client exception text."""
    model = _build_stt_model_with_raising_client()
    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16), frame_rate=24000)

    with trace("stt_redact_test"):
        with pytest.raises(RuntimeError):
            await model.transcribe(
                audio_input,
                STTModelSettings(),
                trace_include_sensitive_data=False,
                trace_include_sensitive_audio_data=False,
            )

    errors = [err for err in _transcription_span_errors() if err is not None]
    assert errors, "expected at least one transcription span error"
    for error in errors:
        assert error["message"] == REDACTED_TOOL_ERROR_MESSAGE
        assert PII_MARKER not in str(error)


@pytest.mark.asyncio
async def test_stt_transcribe_error_kept_when_trace_include_sensitive_data_true() -> None:
    """When the flag is True, the transcription span error message must carry
    the raw OpenAI client exception text."""
    model = _build_stt_model_with_raising_client()
    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16), frame_rate=24000)

    with trace("stt_keep_test"):
        with pytest.raises(RuntimeError):
            await model.transcribe(
                audio_input,
                STTModelSettings(),
                trace_include_sensitive_data=True,
                trace_include_sensitive_audio_data=False,
            )

    errors = [err for err in _transcription_span_errors() if err is not None]
    assert errors, "expected at least one transcription span error"
    for error in errors:
        assert error["message"] == PII_MARKER


# ---------------------------------------------------------------------------
# Sanity: the helper signature remains stable and the redaction constant matches
# what the rest of the codebase expects.
# ---------------------------------------------------------------------------


def test_get_trace_tool_error_signature_and_constant_unchanged() -> None:
    """Guard against accidental signature drift on ``get_trace_tool_error``.

    The four SpanError sites in this PR depend on the helper being callable
    with keyword-only ``trace_include_sensitive_data`` and ``error_message``
    arguments, and on the redaction constant value. If either changes, the
    call sites and tests above must be updated in lockstep.
    """
    assert REDACTED_TOOL_ERROR_MESSAGE == "Tool execution failed. Error details are redacted."

    assert (
        get_trace_tool_error(trace_include_sensitive_data=False, error_message=PII_MARKER)
        == REDACTED_TOOL_ERROR_MESSAGE
    )
    assert (
        get_trace_tool_error(trace_include_sensitive_data=True, error_message=PII_MARKER)
        == PII_MARKER
    )
