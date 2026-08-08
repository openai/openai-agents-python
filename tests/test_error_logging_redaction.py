"""Error-path logging must not leak model/tool payloads when data logging is disabled.

The exception attached to a ``SpanError`` is already redacted based on the tracing
flag, but the sibling ``logger.error`` calls used to log the raw exception (and, for
tool actions, the full traceback) unconditionally. These tests lock in that those log
statements honor ``_debug.DONT_LOG_MODEL_DATA`` / ``_debug.DONT_LOG_TOOL_DATA``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
import threading
import traceback
import warnings
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Any, Literal, cast
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, SkipValidation, ValidationError

import agents._debug as _debug
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    ModelBehaviorError,
    ModelSettings,
    ModelTracing,
    OpenAIResponsesModel,
    OutputGuardrail,
    RunConfig,
    RunContextWrapper,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
    UserError,
    function_tool,
    handoff,
    trace,
)
from agents.agent_output import AgentOutputSchema
from agents.logger import (
    log_model_action_debug,
    log_model_action_error,
    log_model_action_warning,
    log_model_and_tool_action_debug,
    log_model_and_tool_action_error,
    log_model_and_tool_action_warning,
    log_model_and_tool_data_warning,
    log_tool_action_debug,
    log_tool_action_error as log_shared_tool_action_error,
    log_tool_action_warning,
)
from agents.realtime import RealtimeAgent, realtime_handoff
from agents.run_internal.tool_execution import (
    log_tool_action_error,
    resolve_approval_rejection_message,
)
from agents.run_state import _deserialize_items
from agents.tool_context import ToolContext
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.provider import SynchronousMultiTracingProcessor
from agents.tracing.spans import Span
from agents.tracing.traces import Trace

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession

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


class _HostileAttributeWriteException(Exception):
    def __setattr__(self, name: str, value: Any) -> None:
        raise RuntimeError("redacted handling mutated the handler exception")


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


class _HostileValue:
    def __str__(self) -> str:
        raise AssertionError("redacted logging inspected __str__")

    def __repr__(self) -> str:
        raise AssertionError("redacted logging inspected __repr__")


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


def _emit_data_warning_for_location(test_logger) -> None:
    log_model_and_tool_data_warning(
        test_logger,
        "Fixed operational warning",
        diagnostic_message="Warning for %s",
        diagnostic_args=lambda: ("diagnostic-value",),
    )


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


def test_shared_data_warning_does_not_inspect_or_attach_redacted_arguments(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    test_logger = logging.Logger("sensitive-logging-data-warning")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    hostile = _HostileValue()

    log_model_and_tool_data_warning(
        test_logger,
        "Fixed operational warning",
        diagnostic_message="Warning for %s",
        diagnostic_args=lambda: (hostile,),
    )

    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.msg == "Fixed operational warning"
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert hostile not in record.__dict__.values()
    assert logging.Formatter().format(record) == "Fixed operational warning"


def test_shared_data_warning_preserves_diagnostics_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    test_logger = logging.Logger("sensitive-logging-data-warning")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    diagnostic_value = "diagnostic-agent"

    log_model_and_tool_data_warning(
        test_logger,
        "Fixed operational warning",
        diagnostic_message="Warning for %s",
        diagnostic_args=lambda: (diagnostic_value,),
    )

    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.msg == "Warning for %s"
    assert record.args == (diagnostic_value,)
    assert logging.Formatter().format(record) == "Warning for diagnostic-agent"


def test_shared_data_warning_falls_back_when_diagnostic_arguments_fail(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    test_logger = logging.Logger("sensitive-logging-data-warning")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)

    def diagnostic_args() -> tuple[object, ...]:
        raise RuntimeError("SECRET_DIAGNOSTIC_ARGUMENT_FAILURE")

    log_model_and_tool_data_warning(
        test_logger,
        "Fixed operational warning",
        diagnostic_message="Warning for %s",
        diagnostic_args=diagnostic_args,
    )

    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.msg == "Fixed operational warning"
    assert record.args == ()
    assert record.exc_info is None
    assert "SECRET_DIAGNOSTIC_ARGUMENT_FAILURE" not in logging.Formatter().format(record)


def test_shared_data_warning_preserves_direct_caller_location(monkeypatch) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    test_logger = logging.Logger("sensitive-logging-data-warning-location")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)

    _emit_data_warning_for_location(test_logger)

    record = handler.records[0]
    assert Path(record.pathname).resolve() == Path(__file__).resolve()
    assert record.funcName == "_emit_data_warning_for_location"


@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted"),
    [
        (True, False),
        (False, True),
        (True, True),
        (False, False),
    ],
)
@pytest.mark.parametrize(
    ("scenario", "secrets", "redacted_message"),
    [
        (
            "missing_agent",
            ("SECRET_AGENT_NAME",),
            "Agent not found, skipping item",
        ),
        (
            "missing_agent_field",
            ("SECRET_ITEM_TYPE",),
            "Item missing agent field, skipping",
        ),
        (
            "missing_handoff_agents",
            ("SECRET_SOURCE_AGENT", "SECRET_TARGET_AGENT"),
            "Skipping handoff output item: could not resolve agents",
        ),
    ],
)
def test_run_state_deserialization_warnings_follow_both_data_policies(
    monkeypatch,
    model_redacted: bool,
    tool_redacted: bool,
    scenario: str,
    secrets: tuple[str, ...],
    redacted_message: str,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    known_agent = Agent(name="KnownAgent")
    if scenario == "missing_agent":
        item_data = {
            "type": "message_output_item",
            "agent": secrets[0],
            "raw_item": {},
        }
    elif scenario == "missing_agent_field":
        item_data = {
            "type": secrets[0],
            "raw_item": {},
        }
    else:
        item_data = {
            "type": "handoff_output_item",
            "agent": "KnownAgent",
            "source_agent": secrets[0],
            "target_agent": secrets[1],
            "raw_item": {},
        }

    test_logger = logging.Logger("sensitive-logging-run-state")
    handler = _RecordingHandler()
    test_logger.addHandler(handler)
    with patch("agents.run_state.logger", test_logger):
        result = _deserialize_items([item_data], {"KnownAgent": known_agent})

    assert result == []
    assert len(handler.records) == 1
    record = handler.records[0]
    rendered = logging.Formatter().format(record)
    redacted = model_redacted or tool_redacted
    if redacted:
        assert record.msg == redacted_message
        assert record.args == ()
        assert record.exc_info is None
        assert record.exc_text is None
        assert rendered == redacted_message
        for secret in secrets:
            assert secret not in rendered
            assert secret not in record.__dict__.values()
    else:
        for secret in secrets:
            assert secret in rendered


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


_TOOL_ARGUMENT_SECRET = "SECRET_TOOL_ARGUMENT_123"


def _requires_integer_argument(value: int) -> str:
    return str(value)


@pytest.mark.asyncio
async def test_function_tool_validation_error_redacts_payload_when_tool_data_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    tool = function_tool(_requires_integer_argument, failure_error_function=None)
    payload = f'{{"value": "{_TOOL_ARGUMENT_SECRET}"}}'

    with pytest.raises(ModelBehaviorError) as exc_info:
        await tool.on_invoke_tool(
            ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload),
            payload,
        )

    error = exc_info.value
    assert str(error) == f"Invalid JSON input for tool {tool.name}"
    assert _TOOL_ARGUMENT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_function_tool_validation_error_preserves_diagnostics_when_tool_data_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    tool = function_tool(_requires_integer_argument, failure_error_function=None)
    payload = f'{{"value": "{_TOOL_ARGUMENT_SECRET}"}}'

    with pytest.raises(ModelBehaviorError) as exc_info:
        await tool.on_invoke_tool(
            ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload),
            payload,
        )

    error = exc_info.value
    assert _TOOL_ARGUMENT_SECRET in str(error)
    assert isinstance(error.__cause__, ValidationError)


class _AgentToolParameters(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_agent_tool_validation_error_redacts_payload_when_tool_data_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    tool = Agent(name="worker").as_tool(
        tool_name="worker_tool",
        tool_description="Runs the worker agent.",
        parameters=_AgentToolParameters,
        failure_error_function=None,
    )
    payload = f'{{"value": "{_TOOL_ARGUMENT_SECRET}"}}'

    with pytest.raises(ModelBehaviorError) as exc_info:
        await tool.on_invoke_tool(
            ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload),
            payload,
        )

    error = exc_info.value
    assert str(error) == f"Invalid JSON input for tool {tool.name}"
    assert _TOOL_ARGUMENT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_agent_tool_validation_error_preserves_diagnostics_when_tool_data_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    tool = Agent(name="worker").as_tool(
        tool_name="worker_tool",
        tool_description="Runs the worker agent.",
        parameters=_AgentToolParameters,
        failure_error_function=None,
    )
    payload = f'{{"value": "{_TOOL_ARGUMENT_SECRET}"}}'

    with pytest.raises(ModelBehaviorError) as exc_info:
        await tool.on_invoke_tool(
            ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload),
            payload,
        )

    error = exc_info.value
    assert _TOOL_ARGUMENT_SECRET in str(error)
    assert isinstance(error.__cause__, ValidationError)


_MODEL_OUTPUT_SECRET = "SECRET_MODEL_OUTPUT_123"
_SENSITIVE_SCHEMA_SECRET = "SENSITIVE_HANDOFF_SCHEMA_SECRET_4207"
_SENSITIVE_OUTPUT_SCHEMA_SECRET = "SENSITIVE_OUTPUT_SCHEMA_SECRET_4207"
_SensitiveHandoffInput = Literal["SENSITIVE_HANDOFF_SCHEMA_SECRET_4207"]
_SensitiveOutput = Literal["SENSITIVE_OUTPUT_SCHEMA_SECRET_4207"]


class _RequiredOutput(BaseModel):
    answer: str
    count: int


class _PermissiveFallbackOutput(BaseModel):
    payload: SkipValidation[str]
    count: int


def _assert_secret_absent_from_agents_traceback(
    error: BaseException,
    secret: str,
    *,
    require_agents_frames: bool = True,
) -> None:
    traceback_exception = traceback.TracebackException.from_exception(error, capture_locals=True)
    agents_source = (Path(__file__).parents[1] / "src" / "agents").resolve()
    agents_frames = [
        frame
        for frame in traceback_exception.stack
        if Path(frame.filename).resolve().is_relative_to(agents_source)
    ]
    if require_agents_frames:
        assert agents_frames
    for frame in agents_frames:
        assert secret not in "".join((frame.locals or {}).values())


def _agents_traceback_frame_locals(error: BaseException) -> list[dict[str, Any]]:
    agents_source = (Path(__file__).parents[1] / "src" / "agents").resolve()
    frame_locals: list[dict[str, Any]] = []
    traceback_object = error.__traceback__
    while traceback_object is not None:
        if (
            Path(traceback_object.tb_frame.f_code.co_filename)
            .resolve()
            .is_relative_to(agents_source)
        ):
            frame_locals.append(traceback_object.tb_frame.f_locals)
        traceback_object = traceback_object.tb_next
    return frame_locals


def _assert_handoff_closure_absent_from_traceback(
    error: BaseException,
    *,
    callback: Any,
    schema_secret: str,
) -> None:
    for frame_locals in _agents_traceback_frame_locals(error):
        for value in frame_locals.values():
            closure = getattr(value, "__closure__", None)
            if closure is None:
                continue
            closure_values = [cell.cell_contents for cell in closure]
            assert all(item is not callback for item in closure_values)
            assert schema_secret not in repr(closure_values)


@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted", "expected_redacted"),
    [
        (True, False, True),
        (False, True, False),
    ],
)
def test_output_schema_validation_error_follows_model_data_policy(
    monkeypatch: pytest.MonkeyPatch,
    model_redacted: bool,
    tool_redacted: bool,
    expected_redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'

    with pytest.raises(ModelBehaviorError) as exc_info:
        AgentOutputSchema(_RequiredOutput).validate_json(payload)

    error = exc_info.value
    if expected_redacted:
        assert _MODEL_OUTPUT_SECRET not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None
        _assert_secret_absent_from_agents_traceback(
            error,
            _MODEL_OUTPUT_SECRET,
            require_agents_frames=False,
        )
    else:
        assert _MODEL_OUTPUT_SECRET in str(error)
        assert isinstance(error.__cause__, ValidationError)
        assert any(
            frame_locals.get("json_str") == payload
            for frame_locals in _agents_traceback_frame_locals(error)
        )


def test_output_schema_redaction_survives_trace_attachment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'

    with patch(
        "agents.util._json.attach_error_to_current_span",
        side_effect=RuntimeError("trace attachment failed"),
    ):
        with pytest.raises(ModelBehaviorError) as exc_info:
            AgentOutputSchema(_RequiredOutput).validate_json(payload)

    error = exc_info.value
    assert _MODEL_OUTPUT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(
        error,
        _MODEL_OUTPUT_SECRET,
        require_agents_frames=False,
    )


def test_output_schema_redaction_omits_sensitive_schema_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    output_type = cast(type[Any], _SensitiveOutput)

    with pytest.raises(ModelBehaviorError) as exc_info:
        AgentOutputSchema(output_type).validate_json('"invalid"')

    error = exc_info.value
    assert _SENSITIVE_OUTPUT_SCHEMA_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(
        error,
        _SENSITIVE_OUTPUT_SCHEMA_SECRET,
        require_agents_frames=False,
    )


@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted", "expected_redacted"),
    [
        (True, False, True),
        (False, True, True),
        (True, True, True),
        (False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_handoff_input_validation_error_follows_mixed_data_policy(
    monkeypatch: pytest.MonkeyPatch,
    model_redacted: bool,
    tool_redacted: bool,
    expected_redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'
    target = Agent(name="target")
    handoff_calls = 0

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: _RequiredOutput) -> None:
        nonlocal handoff_calls
        handoff_calls += 1

    handoff_obj = handoff(target, input_type=_RequiredOutput, on_handoff=on_handoff)
    with pytest.raises(ModelBehaviorError) as exc_info:
        await handoff_obj.on_invoke_handoff(RunContextWrapper(None), payload)

    assert handoff_calls == 0
    error = exc_info.value
    if expected_redacted:
        assert _MODEL_OUTPUT_SECRET not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None
        _assert_secret_absent_from_agents_traceback(
            error,
            _MODEL_OUTPUT_SECRET,
            require_agents_frames=False,
        )
    else:
        assert _MODEL_OUTPUT_SECRET in str(error)
        assert isinstance(error.__cause__, ValidationError)
        assert any(
            frame_locals.get("input_json") == payload
            for frame_locals in _agents_traceback_frame_locals(error)
        )


@pytest.mark.asyncio
async def test_handoff_redaction_omits_sensitive_schema_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    target = Agent(name="target")
    handoff_calls = 0

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: Any) -> None:
        nonlocal handoff_calls
        handoff_calls += 1

    handoff_obj = handoff(
        target,
        input_type=cast(type[Any], _SensitiveHandoffInput),
        on_handoff=on_handoff,
    )
    with pytest.raises(ModelBehaviorError) as exc_info:
        await handoff_obj.on_invoke_handoff(RunContextWrapper(None), '"invalid"')

    error = exc_info.value
    assert handoff_calls == 0
    assert _SENSITIVE_SCHEMA_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(
        error,
        _SENSITIVE_SCHEMA_SECRET,
        require_agents_frames=False,
    )
    _assert_handoff_closure_absent_from_traceback(
        error,
        callback=on_handoff,
        schema_secret=_SENSITIVE_SCHEMA_SECRET,
    )


@pytest.mark.asyncio
async def test_realtime_handoff_redaction_omits_sensitive_schema_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    target = RealtimeAgent(name="target")
    handoff_calls = 0

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: Any) -> None:
        nonlocal handoff_calls
        handoff_calls += 1

    handoff_obj = realtime_handoff(
        target,
        input_type=cast(type[Any], _SensitiveHandoffInput),
        on_handoff=on_handoff,
    )
    with pytest.raises(ModelBehaviorError) as exc_info:
        await handoff_obj.on_invoke_handoff(RunContextWrapper(None), '"invalid"')

    error = exc_info.value
    assert handoff_calls == 0
    assert _SENSITIVE_SCHEMA_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(
        error,
        _SENSITIVE_SCHEMA_SECRET,
        require_agents_frames=False,
    )
    _assert_handoff_closure_absent_from_traceback(
        error,
        callback=on_handoff,
        schema_secret=_SENSITIVE_SCHEMA_SECRET,
    )


@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted", "expected_redacted"),
    [
        (True, False, True),
        (False, True, True),
        (True, True, True),
        (False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_realtime_handoff_input_validation_error_follows_mixed_data_policy(
    monkeypatch: pytest.MonkeyPatch,
    model_redacted: bool,
    tool_redacted: bool,
    expected_redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'
    target = RealtimeAgent(name="target")
    handoff_calls = 0

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: _RequiredOutput) -> None:
        nonlocal handoff_calls
        handoff_calls += 1

    handoff_obj = realtime_handoff(target, input_type=_RequiredOutput, on_handoff=on_handoff)
    with pytest.raises(ModelBehaviorError) as exc_info:
        await handoff_obj.on_invoke_handoff(RunContextWrapper(None), payload)

    assert handoff_calls == 0
    error = exc_info.value
    if expected_redacted:
        assert _MODEL_OUTPUT_SECRET not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None
        _assert_secret_absent_from_agents_traceback(
            error,
            _MODEL_OUTPUT_SECRET,
            require_agents_frames=False,
        )
    else:
        assert _MODEL_OUTPUT_SECRET in str(error)
        assert isinstance(error.__cause__, ValidationError)
        assert any(
            frame_locals.get("input_json") == payload
            for frame_locals in _agents_traceback_frame_locals(error)
        )


@pytest.mark.asyncio
async def test_run_surfaces_redacted_output_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    model = FakeModel()
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    model.set_next_output([get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    session = SimpleListSession(
        session_id="redacted-run",
        history=[{"role": "user", "content": _MODEL_OUTPUT_SECRET}],
    )

    with pytest.raises(ModelBehaviorError) as exc_info:
        await Runner.run(agent, _MODEL_OUTPUT_SECRET, session=session)

    error = exc_info.value
    frame_locals = _agents_traceback_frame_locals(error)
    assert _MODEL_OUTPUT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(
        error,
        _MODEL_OUTPUT_SECRET,
    )
    assert all(session is not value for frame in frame_locals for value in frame.values())


def test_run_sync_surfaces_redacted_output_validation_error_without_runner_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    model = FakeModel()
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    model.set_next_output([get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    session = SimpleListSession(
        session_id="redacted-run-sync",
        history=[{"role": "user", "content": _MODEL_OUTPUT_SECRET}],
    )

    with pytest.raises(ModelBehaviorError) as exc_info:
        Runner.run_sync(agent, _MODEL_OUTPUT_SECRET, session=session)

    error = exc_info.value
    frame_locals = _agents_traceback_frame_locals(error)
    assert _MODEL_OUTPUT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(
        error,
        _MODEL_OUTPUT_SECRET,
    )
    assert all(session is not value for frame in frame_locals for value in frame.values())


@pytest.mark.asyncio
async def test_run_preserves_diagnostic_wrapper_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    diagnostic_input = "DIAGNOSTIC_RUNNER_INPUT_SECRET"
    model = FakeModel(initial_output=[get_text_message('{"answer": "missing count"}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    session = SimpleListSession(session_id="diagnostic-runner")

    with pytest.raises(ModelBehaviorError) as exc_info:
        await Runner.run(agent, diagnostic_input, session=session)

    error = exc_info.value
    frame_locals = _agents_traceback_frame_locals(error)
    assert any(frame.get("input") == diagnostic_input for frame in frame_locals)
    assert any(frame.get("session") is session for frame in frame_locals)


def test_run_sync_preserves_diagnostic_wrapper_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    diagnostic_input = "DIAGNOSTIC_RUNNER_SYNC_INPUT_SECRET"
    model = FakeModel(initial_output=[get_text_message('{"answer": "missing count"}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    session = SimpleListSession(session_id="diagnostic-runner-sync")

    with pytest.raises(ModelBehaviorError) as exc_info:
        Runner.run_sync(agent, diagnostic_input, session=session)

    error = exc_info.value
    frame_locals = _agents_traceback_frame_locals(error)
    assert any(frame.get("input") == diagnostic_input for frame in frame_locals)
    assert any(frame.get("session") is session for frame in frame_locals)


@pytest.mark.asyncio
async def test_streamed_run_surfaces_redacted_output_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    model = FakeModel()
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    model.set_next_output([get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    result = Runner.run_streamed(agent, "go")

    with pytest.raises(ModelBehaviorError) as exc_info:
        async for _ in result.stream_events():
            pass

    error = exc_info.value
    assert _MODEL_OUTPUT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)


@pytest.mark.parametrize("redacted", [False, True])
@pytest.mark.asyncio
async def test_streamed_run_loop_exception_follows_model_data_policy(
    monkeypatch: pytest.MonkeyPatch,
    redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    model = FakeModel(initial_output=[get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    result = Runner.run_streamed(agent, "go")

    assert result.run_loop_task is not None
    while not result.run_loop_task.done():
        await asyncio.sleep(0)

    error = result.run_loop_exception
    assert isinstance(error, ModelBehaviorError)
    if redacted:
        assert error.run_data is None
        assert error.__cause__ is None
        assert error.__context__ is None
        _assert_secret_absent_from_agents_traceback(
            error,
            _MODEL_OUTPUT_SECRET,
            require_agents_frames=False,
        )
    else:
        assert _MODEL_OUTPUT_SECRET in str(error)
        assert isinstance(error.__cause__, ValidationError)
        assert any(
            _MODEL_OUTPUT_SECRET in repr(frame_locals)
            for frame_locals in _agents_traceback_frame_locals(error)
        )


@pytest.mark.asyncio
async def test_streamed_output_guardrail_omits_run_data_from_redacted_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _agent_output: Any,
    ) -> GuardrailFunctionOutput:
        AgentOutputSchema(_RequiredOutput).validate_json(payload)
        raise AssertionError("validation should fail")  # pragma: no cover

    model = FakeModel(initial_output=[get_text_message(_MODEL_OUTPUT_SECRET)])
    agent = Agent(
        name="A",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    result = Runner.run_streamed(agent, "go")

    with pytest.raises(ModelBehaviorError) as exc_info:
        async for _ in result.stream_events():
            pass

    error = exc_info.value
    assert error.run_data is None
    assert _MODEL_OUTPUT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)


@pytest.mark.parametrize("redacted", [False, True])
@pytest.mark.parametrize("persistence_failure", ["error", "cancelled"])
@pytest.mark.asyncio
async def test_streamed_session_error_after_output_guardrail_respects_redaction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    redacted: bool,
    persistence_failure: Literal["error", "cancelled"],
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    guardrail_failed = False
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'

    class FailingFinalTurnSession(SimpleListSession):
        async def add_items(self, items: list[Any]) -> None:
            if guardrail_failed:
                cause = RuntimeError(f"session save cause: {_MODEL_OUTPUT_SECRET}")
                if persistence_failure == "cancelled":
                    raise asyncio.CancelledError(
                        f"session save cancelled: {_MODEL_OUTPUT_SECRET}"
                    ) from cause
                raise LookupError(f"session save failed: {_MODEL_OUTPUT_SECRET}") from cause
            await super().add_items(items)

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _agent_output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_failed
        guardrail_failed = True
        AgentOutputSchema(_RequiredOutput).validate_json(payload)
        raise AssertionError("validation should fail")  # pragma: no cover

    caplog.set_level(logging.ERROR, logger="openai.agents")
    model = FakeModel(initial_output=[get_text_message(_MODEL_OUTPUT_SECRET)])
    agent = Agent(
        name="A",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    result = Runner.run_streamed(agent, "go", session=FailingFinalTurnSession())
    run_loop_callback_errors: list[BaseException] = []
    run_loop_done = asyncio.Event()

    if redacted and persistence_failure == "cancelled":
        assert result.run_loop_task is not None

        def inspect_run_loop_task(task: asyncio.Task[Any]) -> None:
            try:
                task.result()
            except BaseException as error:
                run_loop_callback_errors.append(error)
            finally:
                run_loop_done.set()

        result.run_loop_task.add_done_callback(inspect_run_loop_task)
    expected_error_type = (
        asyncio.CancelledError
        if persistence_failure == "cancelled"
        else UserError
        if redacted
        else LookupError
    )
    expected_message = (
        "Error details are redacted."
        if redacted
        else "session save cancelled"
        if persistence_failure == "cancelled"
        else "session save failed"
    )

    with pytest.raises(expected_error_type, match=expected_message) as exc_info:
        async for _ in result.stream_events():
            pass

    error = exc_info.value
    guardrail_error = error.__context__

    if redacted:
        assert guardrail_error is None
        assert error.__cause__ is None
        assert _MODEL_OUTPUT_SECRET not in str(error)
        _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)
        for record in caplog.records:
            assert _MODEL_OUTPUT_SECRET not in repr(record.__dict__)
            assert _MODEL_OUTPUT_SECRET not in logging.Formatter().format(record)
            assert record.exc_info is None
    else:
        assert isinstance(guardrail_error, ModelBehaviorError)
        assert error.__cause__ is not None
        assert _MODEL_OUTPUT_SECRET in str(error.__cause__)
        assert _MODEL_OUTPUT_SECRET in str(guardrail_error)
        assert any(
            _MODEL_OUTPUT_SECRET in repr(frame) for frame in _agents_traceback_frame_locals(error)
        )

    if persistence_failure == "cancelled":
        assert result._stored_exception is error
        assert result.run_loop_exception is None
        if redacted:
            await asyncio.wait_for(run_loop_done.wait(), timeout=1)
            assert run_loop_callback_errors == []
    else:
        assert result.run_loop_exception is error
        if redacted:
            assert error.__traceback__ is None


@pytest.mark.asyncio
async def test_streamed_session_hostile_error_after_redacted_output_guardrail_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence_secret = "HOSTILE_SESSION_FAILURE_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    guardrail_failed = False
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'

    class FailingFinalTurnSession(SimpleListSession):
        async def add_items(self, items: list[Any]) -> None:
            if guardrail_failed:
                raise _HostileAttributeWriteException(persistence_secret)
            await super().add_items(items)

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _agent_output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_failed
        guardrail_failed = True
        AgentOutputSchema(_RequiredOutput).validate_json(payload)
        raise AssertionError("validation should fail")  # pragma: no cover

    agent = Agent(
        name="A",
        model=FakeModel(initial_output=[get_text_message(_MODEL_OUTPUT_SECRET)]),
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    result = Runner.run_streamed(agent, "go", session=FailingFinalTurnSession())

    with pytest.raises(UserError, match="Error details are redacted.") as exc_info:
        async for _ in result.stream_events():
            pass

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert persistence_secret not in str(error)
    _assert_secret_absent_from_agents_traceback(error, persistence_secret)
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)


@pytest.mark.asyncio
async def test_streamed_input_guardrail_omits_run_data_from_redacted_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    payload = f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}'

    def input_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _input: str | list[Any],
    ) -> GuardrailFunctionOutput:
        AgentOutputSchema(_RequiredOutput).validate_json(payload)
        raise AssertionError("validation should fail")  # pragma: no cover

    model = FakeModel(initial_output=[get_text_message("unused")])
    agent = Agent(
        name="A",
        model=model,
        input_guardrails=[InputGuardrail(guardrail_function=input_guardrail)],
    )
    result = Runner.run_streamed(agent, _MODEL_OUTPUT_SECRET)

    with pytest.raises(ModelBehaviorError) as exc_info:
        async for _ in result.stream_events():
            pass

    error = exc_info.value
    assert error.run_data is None
    assert _MODEL_OUTPUT_SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_invalid_final_output_handler_receives_detached_redacted_error(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = FakeModel(initial_output=[get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)
    retained_errors: list[ModelBehaviorError] = []

    def recover(data: RunErrorHandlerInput[None]) -> _RequiredOutput:
        assert isinstance(data.error, ModelBehaviorError)
        retained_errors.append(data.error)
        return _RequiredOutput(answer="safe", count=1)

    if streamed:
        result = Runner.run_streamed(
            agent,
            "go",
            error_handlers={"invalid_final_output": recover},
        )
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(
            agent,
            "go",
            error_handlers={"invalid_final_output": recover},
        )

    assert len(retained_errors) == 1
    error = retained_errors[0]
    assert error.__traceback__ is None
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _MODEL_OUTPUT_SECRET not in str(error)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_invalid_final_output_handler_invalid_fallback_preserves_redaction(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    fallback_secret = "INVALID_HANDLER_FALLBACK_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = FakeModel(initial_output=[get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)

    def invalid_fallback(_data: RunErrorHandlerInput[None]) -> dict[str, str]:
        return {"answer": fallback_secret}

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        if streamed:
            result = Runner.run_streamed(
                agent,
                "go",
                error_handlers={"invalid_final_output": invalid_fallback},
            )
            with pytest.raises(UserError) as exc_info:
                async for _ in result.stream_events():
                    pass
        else:
            with pytest.raises(UserError) as exc_info:
                await Runner.run(
                    agent,
                    "go",
                    error_handlers={"invalid_final_output": invalid_fallback},
                )

    error = exc_info.value
    assert not caught_warnings
    assert str(error) == "Error details are redacted."
    assert error.run_data is None
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)
    _assert_secret_absent_from_agents_traceback(error, fallback_secret)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("redacted", [True, False])
@pytest.mark.asyncio
async def test_invalid_final_output_handler_fallback_serialization_follows_redaction_policy(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    redacted: bool,
) -> None:
    fallback_secret = "PERMISSIVE_HANDLER_FALLBACK_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    model = FakeModel(
        initial_output=[
            get_text_message(f'{{"payload": "{_MODEL_OUTPUT_SECRET}", "count": "invalid"}}')
        ]
    )
    agent = Agent(name="A", model=model, output_type=_PermissiveFallbackOutput)

    def permissive_fallback(_data: RunErrorHandlerInput[None]) -> _PermissiveFallbackOutput:
        return _PermissiveFallbackOutput(
            payload=cast(Any, {"secret": fallback_secret}),
            count=1,
        )

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        if streamed:
            streaming_result = Runner.run_streamed(
                agent,
                "go",
                error_handlers={"invalid_final_output": permissive_fallback},
            )
            async for _ in streaming_result.stream_events():
                pass
            actual_final_output = streaming_result.final_output
        else:
            run_result = await Runner.run(
                agent,
                "go",
                error_handlers={"invalid_final_output": permissive_fallback},
            )
            actual_final_output = run_result.final_output

    rendered_warnings = "\n".join(str(warning.message) for warning in caught_warnings)
    if redacted:
        assert not caught_warnings
    else:
        assert fallback_secret in rendered_warnings
    assert actual_final_output == _PermissiveFallbackOutput(
        payload=cast(Any, {"secret": fallback_secret}),
        count=1,
    )


@pytest.mark.parametrize(
    ("streamed", "include_in_history", "redacted"),
    [
        (False, True, True),
        (False, False, True),
        (True, True, True),
        (True, False, True),
        (False, True, False),
    ],
)
@pytest.mark.asyncio
async def test_empty_final_output_handler_fallback_serialization_follows_redaction_policy(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    include_in_history: bool,
    redacted: bool,
) -> None:
    fallback_secret = "EMPTY_HANDLER_FALLBACK_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    model = FakeModel(initial_output=[])
    agent = Agent(name="A", model=model, output_type=_PermissiveFallbackOutput)

    def permissive_fallback(_data: RunErrorHandlerInput[None]) -> RunErrorHandlerResult:
        return RunErrorHandlerResult(
            final_output=_PermissiveFallbackOutput(
                payload=cast(Any, {"secret": fallback_secret}),
                count=1,
            ),
            include_in_history=include_in_history,
        )

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        if streamed:
            streaming_result = Runner.run_streamed(
                agent,
                "go",
                error_handlers={"invalid_final_output": permissive_fallback},
            )
            async for _ in streaming_result.stream_events():
                pass
            actual_final_output = streaming_result.final_output
        else:
            run_result = await Runner.run(
                agent,
                "go",
                error_handlers={"invalid_final_output": permissive_fallback},
            )
            actual_final_output = run_result.final_output

    rendered_warnings = "\n".join(str(warning.message) for warning in caught_warnings)
    if redacted:
        assert not caught_warnings
    else:
        assert fallback_secret in rendered_warnings
    assert actual_final_output == _PermissiveFallbackOutput(
        payload=cast(Any, {"secret": fallback_secret}),
        count=1,
    )


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_invalid_final_output_handler_failure_preserves_redaction(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = FakeModel(initial_output=[get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)

    def fail(data: RunErrorHandlerInput[None]) -> None:
        raise RuntimeError(repr(data.run_data.raw_responses))

    if streamed:
        result = Runner.run_streamed(
            agent,
            "go",
            error_handlers={"invalid_final_output": fail},
        )
        with pytest.raises(UserError) as exc_info:
            async for _ in result.stream_events():
                pass
    else:
        with pytest.raises(UserError) as exc_info:
            await Runner.run(
                agent,
                "go",
                error_handlers={"invalid_final_output": fail},
            )

    error = exc_info.value
    assert str(error) == "Error details are redacted."
    assert error.run_data is None
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_invalid_final_output_handler_hostile_failure_preserves_redaction(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    handler_secret = "HOSTILE_HANDLER_FAILURE_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = FakeModel(initial_output=[get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)

    def fail(_data: RunErrorHandlerInput[None]) -> None:
        raise _HostileAttributeWriteException(handler_secret)

    if streamed:
        result = Runner.run_streamed(
            agent,
            "go",
            error_handlers={"invalid_final_output": fail},
        )
        with pytest.raises(UserError) as exc_info:
            async for _ in result.stream_events():
                pass
    else:
        with pytest.raises(UserError) as exc_info:
            await Runner.run(
                agent,
                "go",
                error_handlers={"invalid_final_output": fail},
            )

    error = exc_info.value
    assert str(error) == "Error details are redacted."
    assert error.run_data is None
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_agents_traceback(error, _MODEL_OUTPUT_SECRET)
    _assert_secret_absent_from_agents_traceback(error, handler_secret)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_invalid_final_output_handler_failure_preserves_diagnostic_context(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    model = FakeModel(initial_output=[get_text_message(f'{{"answer": "{_MODEL_OUTPUT_SECRET}"}}')])
    agent = Agent(name="A", model=model, output_type=_RequiredOutput)

    def fail(_data: RunErrorHandlerInput[None]) -> None:
        raise RuntimeError("handler failed")

    if streamed:
        result = Runner.run_streamed(
            agent,
            "go",
            error_handlers={"invalid_final_output": fail},
        )
        with pytest.raises(RuntimeError, match="handler failed") as exc_info:
            async for _ in result.stream_events():
                pass
    else:
        with pytest.raises(RuntimeError, match="handler failed") as exc_info:
            await Runner.run(
                agent,
                "go",
                error_handlers={"invalid_final_output": fail},
            )

    validation_error = exc_info.value.__context__
    assert isinstance(validation_error, ModelBehaviorError)
    assert _MODEL_OUTPUT_SECRET in str(validation_error)
    assert isinstance(validation_error.__cause__, ValidationError)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("redacted", [False, True])
@pytest.mark.asyncio
async def test_multiturn_output_validation_error_run_data_follows_redaction_policy(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)

    @function_tool
    def record_value(value: str) -> str:
        return "recorded"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    record_value.name,
                    json.dumps({"value": _MODEL_OUTPUT_SECRET}),
                )
            ],
            [get_text_message('{"answer": "missing count"}')],
        ]
    )
    agent = Agent(
        name="A",
        model=model,
        tools=[record_value],
        output_type=_RequiredOutput,
    )

    if streamed:
        result = Runner.run_streamed(agent, "go")
        with pytest.raises(ModelBehaviorError) as exc_info:
            async for _ in result.stream_events():
                pass
    else:
        with pytest.raises(ModelBehaviorError) as exc_info:
            await Runner.run(agent, "go")

    error = exc_info.value
    if redacted:
        assert error.run_data is None
        assert _MODEL_OUTPUT_SECRET not in str(error)
        _assert_secret_absent_from_agents_traceback(
            error,
            _MODEL_OUTPUT_SECRET,
            require_agents_frames=streamed,
        )
    else:
        assert error.run_data is not None
        assert error.run_data.raw_responses
        assert error.run_data.new_items
