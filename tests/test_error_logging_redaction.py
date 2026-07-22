"""Error-path logging must not leak model/tool payloads when data logging is disabled.

The exception attached to a ``SpanError`` is already redacted based on the tracing
flag, but the sibling ``logger.error`` calls used to log the raw exception (and, for
tool actions, the full traceback) unconditionally. These tests lock in that those log
statements honor ``_debug.DONT_LOG_MODEL_DATA`` / ``_debug.DONT_LOG_TOOL_DATA``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from openai import AsyncOpenAI

import agents._debug as _debug
from agents import ModelSettings, ModelTracing, OpenAIResponsesModel, trace
from agents.run_internal.tool_execution import log_tool_action_error

_SECRET = "super secret prompt content"


def _responses_model() -> OpenAIResponsesModel:
    return OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_error_redacts_exception_from_logs(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = _responses_model()

    async def raise_fetch(*args, **kwargs):
        raise ValueError(_SECRET)

    monkeypatch.setattr(model, "_fetch_response", raise_fetch)

    with patch("agents.models.openai_responses.logger") as mock_logger:
        with trace(workflow_name="test"):
            with pytest.raises(ValueError):
                await model.get_response(
                    "instr",
                    "input",
                    ModelSettings(),
                    [],
                    None,
                    [],
                    ModelTracing.ENABLED,
                    previous_response_id=None,
                )

    mock_logger.error.assert_called_once()
    logged = str(mock_logger.error.call_args)
    assert _SECRET not in logged
    assert "ValueError" in logged


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_error_logs_exception_when_model_data_enabled(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    model = _responses_model()

    async def raise_fetch(*args, **kwargs):
        raise ValueError(_SECRET)

    monkeypatch.setattr(model, "_fetch_response", raise_fetch)

    with patch("agents.models.openai_responses.logger") as mock_logger:
        with trace(workflow_name="test"):
            with pytest.raises(ValueError):
                await model.get_response(
                    "instr",
                    "input",
                    ModelSettings(),
                    [],
                    None,
                    [],
                    ModelTracing.ENABLED,
                    previous_response_id=None,
                )

    mock_logger.error.assert_called_once()
    assert _SECRET in str(mock_logger.error.call_args)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_error_redacts_exception_from_logs(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = _responses_model()

    async def raise_fetch(*args, **kwargs):
        raise ValueError(_SECRET)

    monkeypatch.setattr(model, "_fetch_response", raise_fetch)

    with patch("agents.models.openai_responses.logger") as mock_logger:
        with trace(workflow_name="test"):
            with pytest.raises(ValueError):
                async for _ in model.stream_response(
                    "instr",
                    "input",
                    ModelSettings(),
                    [],
                    None,
                    [],
                    ModelTracing.ENABLED,
                    previous_response_id=None,
                ):
                    pass

    mock_logger.error.assert_called_once()
    logged = str(mock_logger.error.call_args)
    assert _SECRET not in logged
    assert "ValueError" in logged


def test_log_tool_action_error_redacts_by_default(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

    with patch("agents.run_internal.tool_execution.logger") as mock_logger:
        log_tool_action_error("Shell executor failed", ValueError("rm -rf /secret/path"))

    mock_logger.error.assert_called_once()
    logged = str(mock_logger.error.call_args)
    assert "/secret/path" not in logged
    assert "ValueError" in logged
    # No traceback either, since it can embed the same sensitive data.
    assert mock_logger.error.call_args.kwargs.get("exc_info") in (None, False)


def test_log_tool_action_error_logs_full_when_tool_data_enabled(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

    with patch("agents.run_internal.tool_execution.logger") as mock_logger:
        log_tool_action_error("Shell executor failed", ValueError("rm -rf /secret/path"))

    mock_logger.error.assert_called_once()
    logged = str(mock_logger.error.call_args)
    assert "/secret/path" in logged
    assert mock_logger.error.call_args.kwargs.get("exc_info") is True
