"""Error-path logging must not leak model/tool payloads when data logging is disabled.

The exception attached to a ``SpanError`` is already redacted based on the tracing
flag, but the sibling ``logger.error`` calls used to log the raw exception (and, for
tool actions, the full traceback) unconditionally. These tests lock in that those log
statements honor ``_debug.DONT_LOG_MODEL_DATA`` / ``_debug.DONT_LOG_TOOL_DATA``.
"""

from __future__ import annotations

import logging
import pickle
import threading
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Any
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
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.provider import SynchronousMultiTracingProcessor
from agents.tracing.spans import Span
from agents.tracing.traces import Trace

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


class _TruthinessException(Exception):
    def __init__(self, *, truthy: bool) -> None:
        super().__init__("diagnostic failure")
        self.truthy = truthy
        self.bool_calls = 0

    def __bool__(self) -> bool:
        self.bool_calls += 1
        if self.truthy:
            raise AssertionError("logging inspected exception truthiness")
        return False


class _FailingTracingProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.str_calls = 0
        self.lock = threading.Lock()

    def __str__(self) -> str:
        self.str_calls += 1
        return "SECRET_TRACE_PROCESSOR_ID"

    def _fail(self) -> None:
        raise ValueError(_SECRET)

    def on_trace_start(self, trace: Trace) -> None:
        self._fail()

    def on_trace_end(self, trace: Trace) -> None:
        self._fail()

    def on_span_start(self, span: Span[Any]) -> None:
        self._fail()

    def on_span_end(self, span: Span[Any]) -> None:
        self._fail()

    def shutdown(self) -> None:
        self._fail()

    def force_flush(self) -> None:
        self._fail()


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


@pytest.mark.parametrize(
    "helper",
    [log_shared_tool_action_error, log_tool_action_warning],
)
@pytest.mark.parametrize("truthy", [False, True], ids=["falsey", "hostile_bool"])
def test_shared_error_helpers_do_not_evaluate_exception_truthiness(
    monkeypatch,
    helper,
    truthy: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    test_logger = logging.Logger("sensitive-logging-exception-truthiness")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    error = _TruthinessException(truthy=truthy)

    try:
        raise error
    except _TruthinessException:
        helper(test_logger, "Tool failed", error)

    record = handler.records[0]
    assert error.bool_calls == 0
    assert record.exc_info is not None
    assert record.exc_info[0] is type(error)
    assert record.exc_info[1] is error
    assert record.exc_info[2] is error.__traceback__


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
    assert ("openai_agents_diagnostic_context" in record.__dict__) is not redacted
    if not redacted:
        assert record.__dict__["openai_agents_diagnostic_context"] == {"sandbox_id": _SECRET}


def test_shared_error_helper_ignores_diagnostic_extra_failure(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    test_logger = logging.Logger("sensitive-logging-diagnostic-extra-failure")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    error = RuntimeError("original failure")

    def diagnostic_extra() -> dict[str, object]:
        raise AttributeError("metadata failure")

    log_tool_action_warning(
        test_logger,
        "Tool failed",
        error,
        diagnostic_extra=diagnostic_extra,
    )

    record = handler.records[0]
    assert "openai_agents_diagnostic_context" not in record.__dict__
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert "original failure" in logging.Formatter().format(record)


@pytest.mark.parametrize(
    "operation",
    [
        "on_trace_start",
        "on_trace_end",
        "on_span_start",
        "on_span_end",
        "force_flush",
        "shutdown",
    ],
)
@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted"),
    [(True, False), (False, True), (False, False)],
)
def test_trace_processor_failure_identity_follows_both_data_policies(
    monkeypatch,
    operation: str,
    model_redacted: bool,
    tool_redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    test_logger = logging.Logger("sensitive-logging-trace-processor", level=logging.DEBUG)
    test_logger.propagate = False
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    failing = _FailingTracingProcessor()
    multi = SynchronousMultiTracingProcessor()
    multi.add_tracing_processor(failing)

    with patch("agents.tracing.provider.logger", test_logger):
        if operation.startswith(("on_trace", "on_span")):
            getattr(multi, operation)(object())
        else:
            getattr(multi, operation)()

    record = next(record for record in handler.records if record.levelno == logging.ERROR)
    redacted = model_redacted or tool_redacted
    if redacted:
        assert "openai_agents_diagnostic_context" not in record.__dict__
        assert failing not in record.__dict__.values()
        assert record.exc_info is None
        assert _SECRET not in logging.Formatter().format(record)
        assert failing.str_calls == 0
    else:
        processor_identity = record.__dict__["openai_agents_diagnostic_context"]["trace_processor"]
        assert isinstance(processor_identity, str)
        assert type(failing).__module__ in processor_identity
        assert type(failing).__qualname__ in processor_identity
        assert f"{id(failing):x}" in processor_identity
        prepared = QueueHandler(SimpleQueue()).prepare(record)
        pickle.dumps(prepared)
        assert record.exc_info is not None
        assert record.exc_info[1] is not None
        assert _SECRET in logging.Formatter().format(record)


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
    exc_info = mock_logger.error.call_args.kwargs.get("exc_info")
    assert isinstance(exc_info, tuple)
    assert exc_info[0] is ValueError
    assert isinstance(exc_info[1], ValueError)
    assert exc_info[2] is None


@pytest.mark.asyncio
async def test_approval_rejection_formatter_error_redacts_exception(monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

    def boom(_args):
        raise ValueError("formatter blew up SECRET_FMT_123")

    tool_name = "SECRET_FORMATTER_TOOL_NAME"
    result = await resolve_approval_rejection_message(
        context_wrapper=RunContextWrapper(context=None),
        run_config=RunConfig(tool_error_formatter=boom),
        tool_type="function",
        tool_name=tool_name,
        call_id="call_1",
    )

    assert isinstance(result, str) and result
    record = next(
        record for record in caplog.records if "Tool error formatter failed" in record.getMessage()
    )
    assert record.msg == "%s"
    assert record.args == ("Tool error formatter failed",)
    assert record.exc_info is None
    assert "openai_agents_diagnostic_context" not in record.__dict__
    assert tool_name not in caplog.text
    assert "SECRET_FMT_123" not in caplog.text


@pytest.mark.asyncio
async def test_approval_rejection_formatter_error_logs_full_when_enabled(
    monkeypatch, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

    def boom(_args):
        raise ValueError("formatter blew up SECRET_FMT_123")

    tool_name = "diagnostic_tool"
    await resolve_approval_rejection_message(
        context_wrapper=RunContextWrapper(context=None),
        run_config=RunConfig(tool_error_formatter=boom),
        tool_type="function",
        tool_name=tool_name,
        call_id="call_1",
    )

    record = next(
        record for record in caplog.records if "Tool error formatter failed" in record.getMessage()
    )
    assert record.__dict__["openai_agents_diagnostic_context"] == {"tool_name": tool_name}
    assert record.exc_info is not None
    assert "SECRET_FMT_123" in caplog.text
