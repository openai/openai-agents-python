"""Error-path logging must not leak model/tool payloads when data logging is disabled.

The exception attached to a ``SpanError`` is already redacted based on the tracing
flag, but the sibling ``logger.error`` calls used to log the raw exception (and, for
tool actions, the full traceback) unconditionally. These tests lock in that those log
statements honor ``_debug.DONT_LOG_MODEL_DATA`` / ``_debug.DONT_LOG_TOOL_DATA``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI

import agents._debug as _debug
from agents import (
    ModelSettings,
    ModelTracing,
    OpenAIResponsesModel,
    RunConfig,
    RunContextWrapper,
    trace,
)
from agents.logger import (
    log_model_action_debug,
    log_model_action_error,
    log_model_action_warning,
    log_model_and_tool_action_debug,
    log_model_and_tool_action_error,
    log_model_and_tool_action_warning,
    log_tool_action_debug,
    log_tool_action_error as log_shared_tool_action_error,
    log_tool_action_warning,
)
from agents.run_internal.tool_execution import (
    log_tool_action_error,
    resolve_approval_rejection_message,
)

_SECRET = "super secret prompt content"


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _HostileException(Exception):
    def __str__(self) -> str:
        raise AssertionError("redacted logging inspected __str__")

    def __repr__(self) -> str:
        raise AssertionError("redacted logging inspected __repr__")

    def __getattribute__(self, name: str):
        if name in {"__class__", "__traceback__"}:
            raise AssertionError(f"redacted logging inspected {name}")
        return super().__getattribute__(name)


def _emit_shared_error_for_location(test_logger, helper) -> None:
    helper(test_logger, "Fixed operational message", ValueError("failure"))


def _emit_tool_execution_error_for_location() -> None:
    log_tool_action_error("Fixed operational message", ValueError("failure"))


def _responses_model() -> OpenAIResponsesModel:
    return OpenAIResponsesModel(
        model="test-model",
        openai_client=AsyncOpenAI(
            api_key="test",
            http_client=httpx.AsyncClient(trust_env=False),
        ),
    )


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
    assert "ValueError" not in logged
    assert "Error getting response" in logged


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
    assert "ValueError" not in logged
    assert "Error streaming response" in logged


def test_log_tool_action_error_redacts_by_default(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

    with patch("agents.run_internal.tool_execution.logger") as mock_logger:
        log_tool_action_error("Shell executor failed", ValueError("rm -rf /secret/path"))

    mock_logger.error.assert_called_once()
    assert mock_logger.error.call_args.args == ("%s", "Shell executor failed")
    # No traceback either, since it can embed the same sensitive data.
    assert mock_logger.error.call_args.kwargs.get("exc_info") in (None, False)


@pytest.mark.parametrize(
    ("helper", "model_flag", "tool_flag"),
    [
        (log_model_action_error, True, False),
        (log_model_action_debug, True, False),
        (log_model_action_warning, True, False),
        (log_tool_action_debug, False, True),
        (log_shared_tool_action_error, False, True),
        (log_tool_action_warning, False, True),
        (log_model_and_tool_action_error, True, False),
        (log_model_and_tool_action_error, False, True),
        (log_model_and_tool_action_debug, True, False),
        (log_model_and_tool_action_warning, False, True),
    ],
)
def test_shared_error_helpers_do_not_inspect_or_attach_redacted_exceptions(
    monkeypatch,
    helper,
    model_flag: bool,
    tool_flag: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_flag)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_flag)
    test_logger = logging.Logger("sensitive-logging-redacted")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    hostile = _HostileException()

    helper(test_logger, "Fixed operational message", hostile)

    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.msg == "%s"
    assert record.args == ("Fixed operational message",)
    assert record.exc_info is None
    assert record.exc_text is None
    assert hostile not in record.__dict__.values()
    assert logging.Formatter().format(record) == "Fixed operational message"


def test_shared_error_helper_preserves_diagnostics_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    test_logger = logging.Logger("sensitive-logging-diagnostic")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    error = ValueError(_SECRET)

    log_shared_tool_action_error(test_logger, "Tool failed", error)

    record = handler.records[0]
    assert isinstance(record.args, tuple)
    assert error in record.args
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert _SECRET in logging.Formatter().format(record)


@pytest.mark.parametrize("redacted", [True, False])
def test_shared_error_helper_conditionally_attaches_diagnostic_extra(
    monkeypatch, redacted: bool
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    test_logger = logging.Logger("sensitive-logging-diagnostic-extra")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    extra_calls = 0

    def diagnostic_extra() -> dict[str, object]:
        nonlocal extra_calls
        extra_calls += 1
        return {"sandbox_id": _SECRET}

    log_tool_action_warning(
        test_logger,
        "Tool failed",
        ValueError("failure"),
        diagnostic_extra=diagnostic_extra,
    )

    record = handler.records[0]
    assert extra_calls == (0 if redacted else 1)
    assert ("sandbox_id" in record.__dict__) is not redacted
    if not redacted:
        assert record.__dict__["sandbox_id"] == _SECRET


@pytest.mark.parametrize(
    "helper",
    [log_shared_tool_action_error, log_tool_action_warning],
)
def test_shared_error_helpers_preserve_direct_caller_location(monkeypatch, helper) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    test_logger = logging.Logger("sensitive-logging-location")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)

    _emit_shared_error_for_location(test_logger, helper)

    record = handler.records[0]
    assert Path(record.pathname).resolve() == Path(__file__).resolve()
    assert record.funcName == "_emit_shared_error_for_location"


def test_tool_execution_error_helper_preserves_external_caller_location(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    test_logger = logging.Logger("sensitive-logging-wrapped-location")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)

    with patch("agents.run_internal.tool_execution.logger", test_logger):
        _emit_tool_execution_error_for_location()

    record = handler.records[0]
    assert Path(record.pathname).resolve() == Path(__file__).resolve()
    assert record.funcName == "_emit_tool_execution_error_for_location"


def test_shared_error_helper_drops_exception_chains_and_notes(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    test_logger = logging.Logger("sensitive-logging-chain")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    cause = ValueError(f"{_SECRET} cause")
    error = RuntimeError(f"{_SECRET} outer")
    error.__cause__ = cause
    if hasattr(error, "add_note"):
        error.add_note(f"{_SECRET} note")
    else:
        error.__notes__ = [f"{_SECRET} note"]

    log_model_action_error(test_logger, "Model failed", error)

    record = handler.records[0]
    assert record.exc_info is None
    assert record.exc_text is None
    assert error not in record.__dict__.values()
    assert _SECRET not in logging.Formatter().format(record)


def test_log_tool_action_error_logs_full_when_tool_data_enabled(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

    with patch("agents.run_internal.tool_execution.logger") as mock_logger:
        log_tool_action_error("Shell executor failed", ValueError("rm -rf /secret/path"))

    mock_logger.error.assert_called_once()
    logged = str(mock_logger.error.call_args)
    assert "/secret/path" in logged
    assert isinstance(mock_logger.error.call_args.kwargs.get("exc_info"), ValueError)


@pytest.mark.asyncio
async def test_approval_rejection_formatter_error_redacts_exception(monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

    def boom(_args):
        raise ValueError("formatter blew up SECRET_FMT_123")

    result = await resolve_approval_rejection_message(
        context_wrapper=RunContextWrapper(context=None),
        run_config=RunConfig(tool_error_formatter=boom),
        tool_type="function",
        tool_name="my_tool",
        call_id="call_1",
    )

    assert isinstance(result, str) and result
    assert "Tool error formatter failed for my_tool" in caplog.text
    assert "SECRET_FMT_123" not in caplog.text


@pytest.mark.asyncio
async def test_approval_rejection_formatter_error_logs_full_when_enabled(
    monkeypatch, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

    def boom(_args):
        raise ValueError("formatter blew up SECRET_FMT_123")

    await resolve_approval_rejection_message(
        context_wrapper=RunContextWrapper(context=None),
        run_config=RunConfig(tool_error_formatter=boom),
        tool_type="function",
        tool_name="my_tool",
        call_id="call_1",
    )

    assert "SECRET_FMT_123" in caplog.text
