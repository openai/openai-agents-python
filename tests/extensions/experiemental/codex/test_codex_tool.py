from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass, fields
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent, function_tool
from agents.exceptions import ModelBehaviorError, UserError
from agents.extensions.experimental.codex import (
    Codex,
    CodexToolOptions,
    CodexToolResult,
    CodexToolStreamEvent,
    Usage,
    codex_tool,
)
from agents.extensions.experimental.codex.codex_tool import CodexToolInputItem
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext
from agents.tracing import function_span, trace
from tests.testing_processor import SPAN_PROCESSOR_TESTING

codex_tool_module = importlib.import_module("agents.extensions.experimental.codex.codex_tool")


class CodexMockState:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.thread_id: str | None = "thread-1"
        self.last_turn_options: Any = None
        self.start_calls = 0
        self.resume_calls = 0
        self.last_resumed_thread_id: str | None = None
        self.options: Any = None


class FakeThread:
    def __init__(self, state: CodexMockState) -> None:
        self._state = state
        self.id: str | None = None

    async def run_streamed(self, _input: Any, turn_options: Any = None) -> Any:
        self._state.last_turn_options = turn_options
        self.id = self._state.thread_id

        async def event_stream() -> Any:
            for event in self._state.events:
                yield event

        return SimpleNamespace(events=event_stream())


class FakeCodex:
    def __init__(self, state: CodexMockState, options: Any = None) -> None:
        self._state = state
        self._state.options = options

    def start_thread(self, _options: Any = None) -> FakeThread:
        self._state.start_calls += 1
        return FakeThread(self._state)

    def resume_thread(self, _thread_id: str, _options: Any = None) -> FakeThread:
        self._state.resume_calls += 1
        self._state.last_resumed_thread_id = _thread_id
        return FakeThread(self._state)


def test_codex_tool_kw_matches_codex_tool_options() -> None:
    signature = inspect.signature(codex_tool)
    kw_only = [
        param.name
        for param in signature.parameters.values()
        if param.kind == inspect.Parameter.KEYWORD_ONLY
    ]
    option_fields = [field.name for field in fields(CodexToolOptions)]
    assert kw_only == option_fields


@pytest.mark.asyncio
async def test_codex_tool_streams_events_and_updates_usage() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {"id": "reason-1", "type": "reasoning", "text": "Initial reasoning"},
        },
        {
            "type": "item.updated",
            "item": {"id": "reason-1", "type": "reasoning", "text": "Refined reasoning"},
        },
        {
            "type": "item.completed",
            "item": {"id": "reason-1", "type": "reasoning", "text": "Final reasoning"},
        },
        {
            "type": "item.started",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "pytest",
                "aggregated_output": "",
                "status": "in_progress",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "pytest",
                "aggregated_output": "Running tests",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "pytest",
                "aggregated_output": "All good",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "gitmcp",
                "tool": "search_codex_code",
                "arguments": {"query": "foo"},
                "status": "in_progress",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "gitmcp",
                "tool": "search_codex_code",
                "arguments": {"query": "foo"},
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "gitmcp",
                "tool": "search_codex_code",
                "arguments": {"query": "foo"},
                "status": "completed",
                "result": {"content": [], "structured_content": None},
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex finished."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 1, "output_tokens": 5},
        },
    ]

    tool = codex_tool(CodexToolOptions(codex=cast(Codex, FakeCodex(state))))
    input_json = '{"inputs": [{"type": "text", "text": "Diagnose failure", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with trace("codex-test"):
        with function_span(tool.name):
            result = await tool.on_invoke_tool(context, input_json)

    assert isinstance(result, CodexToolResult)
    assert result.thread_id == "thread-1"
    assert result.response == "Codex finished."
    assert result.usage == Usage(
        input_tokens=10,
        cached_input_tokens=1,
        output_tokens=5,
    )

    assert context.usage.total_tokens == 15
    assert context.usage.requests == 1

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    function_span_obj = next(
        span
        for span in spans
        if span.span_data.type == "function" and span.span_data.name == tool.name
    )

    custom_spans = [span for span in spans if span.span_data.type == "custom"]
    assert len(custom_spans) == 3

    for span in custom_spans:
        assert span.parent_id == function_span_obj.span_id

    reasoning_span = next(span for span in custom_spans if span.span_data.name == "Codex reasoning")
    assert reasoning_span.span_data.data["text"] == "Final reasoning"

    command_span = next(
        span for span in custom_spans if span.span_data.name == "Codex command execution"
    )
    assert command_span.span_data.data["command"] == "pytest"
    assert command_span.span_data.data["status"] == "completed"
    assert command_span.span_data.data["output"] == "All good"
    assert command_span.span_data.data["exit_code"] == 0

    mcp_span = next(span for span in custom_spans if span.span_data.name == "Codex MCP tool call")
    assert mcp_span.span_data.data["server"] == "gitmcp"
    assert mcp_span.span_data.data["tool"] == "search_codex_code"
    assert mcp_span.span_data.data["status"] == "completed"


@pytest.mark.asyncio
async def test_codex_tool_keeps_command_output_when_completed_missing_output() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.started",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls",
                "aggregated_output": "",
                "status": "in_progress",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls",
                "aggregated_output": "first output",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex finished."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(CodexToolOptions(codex=cast(Codex, FakeCodex(state))))
    input_json = '{"inputs": [{"type": "text", "text": "List files", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with trace("codex-test"):
        with function_span(tool.name):
            await tool.on_invoke_tool(context, input_json)

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    command_span = next(span for span in spans if span.span_data.name == "Codex command execution")

    assert command_span.span_data.data["output"] == "first output"


@pytest.mark.asyncio
async def test_codex_tool_defaults_to_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    class CaptureCodex(FakeCodex):
        def __init__(self, options: Any = None) -> None:
            super().__init__(state, options)

    monkeypatch.setattr(codex_tool_module, "Codex", CaptureCodex)

    tool = codex_tool()
    input_json = '{"inputs": [{"type": "text", "text": "Check default api key", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert state.options is not None
    assert getattr(state.options, "api_key", None) == "openai-key"


@pytest.mark.asyncio
async def test_codex_tool_accepts_codex_options_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    class CaptureCodex(FakeCodex):
        def __init__(self, options: Any = None) -> None:
            super().__init__(state, options)

    monkeypatch.setattr(codex_tool_module, "Codex", CaptureCodex)

    tool = codex_tool({"codex_options": {"api_key": "from-options"}})
    input_json = '{"inputs": [{"type": "text", "text": "Check dict options", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert state.options is not None
    assert getattr(state.options, "api_key", None) == "from-options"


@pytest.mark.asyncio
async def test_codex_tool_accepts_output_schema_descriptor() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    descriptor = {
        "title": "Summary",
        "properties": [
            {
                "name": "summary",
                "description": "Short summary",
                "schema": {"type": "string", "description": "Summary field"},
            }
        ],
    }

    tool = codex_tool(
        CodexToolOptions(codex=cast(Codex, FakeCodex(state)), output_schema=descriptor)
    )
    input_json = '{"inputs": [{"type": "text", "text": "Check schema", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    output_schema = state.last_turn_options.output_schema
    assert output_schema["type"] == "object"
    assert output_schema["additionalProperties"] is False
    assert output_schema["properties"]["summary"]["type"] == "string"
    assert output_schema["properties"]["summary"]["description"] == "Short summary"
    assert output_schema["required"] == []


@pytest.mark.asyncio
async def test_codex_tool_accepts_dict_options() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    options_dict: dict[str, Any] = {
        "codex": cast(Codex, FakeCodex(state)),
        "sandbox_mode": "read-only",
    }

    tool = codex_tool(options_dict)
    input_json = '{"inputs": [{"type": "text", "text": "Check dict options", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    result = await tool.on_invoke_tool(context, input_json)

    assert isinstance(result, CodexToolResult)
    assert result.response == "Codex done."


@pytest.mark.asyncio
async def test_codex_tool_accepts_keyword_options(monkeypatch: pytest.MonkeyPatch) -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    class CaptureCodex(FakeCodex):
        def __init__(self, options: Any = None) -> None:
            super().__init__(state, options)

    monkeypatch.setattr(codex_tool_module, "Codex", CaptureCodex)

    tool = codex_tool(name="codex_keyword", codex_options={"api_key": "from-kwargs"})
    input_json = '{"inputs": [{"type": "text", "text": "Check keyword options", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert tool.name == "codex_keyword"
    assert state.options is not None
    assert getattr(state.options, "api_key", None) == "from-kwargs"


def test_codex_tool_truncates_span_values() -> None:
    value = {"payload": "x" * 200}
    truncated = codex_tool_module._truncate_span_value(value, 40)

    assert isinstance(truncated, dict)
    assert truncated["truncated"] is True
    assert truncated["original_length"] > 40
    preview = truncated["preview"]
    assert isinstance(preview, str)
    assert len(preview) <= 40


def test_codex_tool_enforces_span_data_budget() -> None:
    data = {
        "command": "run",
        "output": "x" * 5000,
        "arguments": {"payload": "y" * 5000},
    }
    trimmed = codex_tool_module._enforce_span_data_budget(data, 512)

    assert "command" in trimmed
    assert trimmed["command"]
    assert "output" in trimmed
    assert "arguments" in trimmed
    assert codex_tool_module._json_char_size(trimmed) <= 512


def test_codex_tool_keeps_output_preview_with_budget() -> None:
    data = {"output": "x" * 1000}
    trimmed = codex_tool_module._enforce_span_data_budget(data, 120)

    assert "output" in trimmed
    assert isinstance(trimmed["output"], str)
    assert trimmed["output"]
    assert codex_tool_module._json_char_size(trimmed) <= 120


def test_codex_tool_prioritizes_arguments_over_large_results() -> None:
    data = {"arguments": {"foo": "bar"}, "result": "x" * 2000}
    trimmed = codex_tool_module._enforce_span_data_budget(data, 200)

    assert trimmed["arguments"] == codex_tool_module._stringify_span_value({"foo": "bar"})
    assert "result" in trimmed
    assert codex_tool_module._json_char_size(trimmed) <= 200


@pytest.mark.asyncio
async def test_codex_tool_passes_idle_timeout_seconds() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            default_turn_options={"idle_timeout_seconds": 3.5},
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Check timeout option", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert state.last_turn_options is not None
    assert state.last_turn_options.idle_timeout_seconds == 3.5


@pytest.mark.asyncio
async def test_codex_tool_persists_session() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            persist_session=True,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "First call", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)
    await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 1
    assert state.resume_calls == 0


@pytest.mark.asyncio
async def test_codex_tool_accepts_thread_id_from_tool_input() -> None:
    state = CodexMockState()
    state.thread_id = "thread-from-input"
    state.events = [
        {"type": "thread.started", "thread_id": "thread-from-input"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(CodexToolOptions(codex=cast(Codex, FakeCodex(state))))
    input_json = (
        '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}], '
        '"thread_id": "thread-xyz"}'
    )
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    result = await tool.on_invoke_tool(context, input_json)

    assert isinstance(result, CodexToolResult)
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-xyz"
    assert result.thread_id == "thread-from-input"


@pytest.mark.asyncio
async def test_codex_tool_uses_run_context_thread_id_and_persists_latest() -> None:
    state = CodexMockState()
    state.thread_id = "thread-next"
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            run_context_thread_id_key="codex_agent_thread_id",
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context = {"codex_agent_thread_id": "thread-prev"}
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    result = await tool.on_invoke_tool(context, input_json)

    assert isinstance(result, CodexToolResult)
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-prev"
    assert run_context["codex_agent_thread_id"] == "thread-next"
    assert result.thread_id == "thread-next"


@pytest.mark.asyncio
async def test_codex_tool_persists_thread_started_id_when_thread_object_id_is_none() -> None:
    state = CodexMockState()
    state.thread_id = None
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            run_context_thread_id_key="codex_agent_thread_id",
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context: dict[str, str] = {}
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    first_result = await tool.on_invoke_tool(context, input_json)
    second_result = await tool.on_invoke_tool(context, input_json)

    assert isinstance(first_result, CodexToolResult)
    assert isinstance(second_result, CodexToolResult)
    assert first_result.thread_id == "thread-next"
    assert second_result.thread_id == "thread-next"
    assert run_context["codex_agent_thread_id"] == "thread-next"
    assert state.start_calls == 1
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-next"


@pytest.mark.asyncio
async def test_codex_tool_persists_thread_id_for_recoverable_turn_failure() -> None:
    state = CodexMockState()
    state.thread_id = None
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {"type": "turn.failed", "error": {"message": "boom"}},
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            run_context_thread_id_key="codex_agent_thread_id",
            failure_error_function=lambda _ctx, _exc: "handled",
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context: dict[str, str] = {}
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    first_result = await tool.on_invoke_tool(context, input_json)
    second_result = await tool.on_invoke_tool(context, input_json)

    assert first_result == "handled"
    assert second_result == "handled"
    assert run_context["codex_agent_thread_id"] == "thread-next"
    assert state.start_calls == 1
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-next"


@pytest.mark.asyncio
async def test_codex_tool_persists_thread_id_for_raised_turn_failure() -> None:
    state = CodexMockState()
    state.thread_id = None
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {"type": "turn.failed", "error": {"message": "boom"}},
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            run_context_thread_id_key="codex_agent_thread_id",
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context: dict[str, str] = {}
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match="Codex turn failed: boom"):
        await tool.on_invoke_tool(context, input_json)

    assert run_context["codex_agent_thread_id"] == "thread-next"

    with pytest.raises(UserError, match="Codex turn failed: boom"):
        await tool.on_invoke_tool(context, input_json)

    assert run_context["codex_agent_thread_id"] == "thread-next"
    assert state.start_calls == 1
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-next"


@pytest.mark.asyncio
async def test_codex_tool_falls_back_to_call_thread_id_when_thread_object_id_is_none() -> None:
    state = CodexMockState()
    state.thread_id = None
    state.events = [
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            parameters=codex_tool_module.CodexToolParameters,
            use_run_context_thread_id=True,
        )
    )
    first_input_json = (
        '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}], '
        '"thread_id": "thread-explicit"}'
    )
    second_input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context: dict[str, str] = {}
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=first_input_json,
    )

    first_result = await tool.on_invoke_tool(context, first_input_json)
    second_result = await tool.on_invoke_tool(context, second_input_json)

    assert isinstance(first_result, CodexToolResult)
    assert isinstance(second_result, CodexToolResult)
    assert first_result.thread_id == "thread-explicit"
    assert second_result.thread_id == "thread-explicit"
    assert run_context["codex_thread_id"] == "thread-explicit"
    assert state.start_calls == 0
    assert state.resume_calls == 2
    assert state.last_resumed_thread_id == "thread-explicit"


@pytest.mark.asyncio
async def test_codex_tool_uses_run_context_thread_id_with_pydantic_context() -> None:
    class RunContext(BaseModel):
        model_config = ConfigDict(extra="forbid")
        user_id: str

    state = CodexMockState()
    state.thread_id = "thread-next"
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context = RunContext(user_id="abc")
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)
    await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 1
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-next"
    assert run_context.__dict__["codex_thread_id"] == "thread-next"


@pytest.mark.asyncio
async def test_codex_tool_uses_pydantic_context_field_matching_thread_id_key() -> None:
    class RunContext(BaseModel):
        model_config = ConfigDict(extra="forbid")
        user_id: str
        codex_thread_id: str | None = None

    state = CodexMockState()
    state.thread_id = "thread-next"
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context = RunContext(user_id="abc", codex_thread_id="thread-prev")
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 1
    assert state.last_resumed_thread_id == "thread-prev"
    assert run_context.codex_thread_id == "thread-next"


@pytest.mark.asyncio
async def test_codex_tool_default_run_context_key_follows_tool_name() -> None:
    state = CodexMockState()
    state.thread_id = "thread-next"
    state.events = [
        {"type": "thread.started", "thread_id": "thread-next"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
        ),
        name="codex_engineer",
    )
    input_json = '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}]}'
    run_context = {"codex_thread_id_engineer": "thread-prev"}
    context = ToolContext(
        context=run_context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert state.last_resumed_thread_id == "thread-prev"
    assert run_context["codex_thread_id_engineer"] == "thread-next"


def test_codex_tool_rejects_custom_name_without_codex_prefix() -> None:
    with pytest.raises(UserError, match='must be "codex" or start with "codex_"'):
        codex_tool(name="engineer")


def test_codex_tool_allows_non_alnum_suffix_when_run_context_thread_id_disabled() -> None:
    tool = codex_tool(name="codex_a-b")
    assert tool.name == "codex_a-b"


def test_codex_tool_rejects_lossy_default_run_context_thread_id_key_suffix() -> None:
    with pytest.raises(UserError, match="run_context_thread_id_key"):
        codex_tool(name="codex_a-b", use_run_context_thread_id=True)


@pytest.mark.asyncio
async def test_codex_tool_tool_input_thread_id_overrides_run_context_thread_id() -> None:
    state = CodexMockState()
    state.thread_id = "thread-from-tool-input"
    state.events = [
        {"type": "thread.started", "thread_id": "thread-from-tool-input"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            parameters=codex_tool_module.CodexToolParameters,
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = (
        '{"inputs": [{"type": "text", "text": "Continue thread", "path": ""}], '
        '"thread_id": "thread-from-args"}'
    )
    context = ToolContext(
        context={"codex_thread_id": "thread-from-context"},
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    await tool.on_invoke_tool(context, input_json)

    assert state.last_resumed_thread_id == "thread-from-args"


def test_codex_tool_run_context_mode_hides_thread_id_in_default_parameters() -> None:
    tool = codex_tool(use_run_context_thread_id=True)
    assert "thread_id" not in tool.params_json_schema["properties"]


@pytest.mark.asyncio
async def test_codex_tool_duplicate_names_fail_fast() -> None:
    agent = Agent(
        name="test",
        tools=[
            codex_tool(),
            codex_tool(),
        ],
    )

    with pytest.raises(UserError, match="Duplicate Codex tool names found"):
        await agent.get_all_tools(RunContextWrapper(context=None))


@pytest.mark.asyncio
async def test_codex_tool_name_collision_with_other_tool_fails_fast() -> None:
    @function_tool(name_override="codex")
    def other_tool() -> str:
        return "ok"

    agent = Agent(
        name="test",
        tools=[
            codex_tool(),
            other_tool,
        ],
    )

    with pytest.raises(UserError, match="Duplicate Codex tool names found"):
        await agent.get_all_tools(RunContextWrapper(context=None))


@pytest.mark.asyncio
async def test_codex_tool_run_context_thread_id_requires_mutable_context() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "No context", "path": ""}]}'
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match="use_run_context_thread_id=True"):
        await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 0


@pytest.mark.asyncio
async def test_codex_tool_run_context_thread_id_rejects_immutable_mapping_context() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Immutable context", "path": ""}]}'
    context = ToolContext(
        context=MappingProxyType({"codex_thread_id": "thread-prev"}),
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match="use_run_context_thread_id=True"):
        await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 0


@pytest.mark.asyncio
async def test_codex_tool_run_context_thread_id_rejects_frozen_pydantic_context() -> None:
    class FrozenRunContext(BaseModel):
        model_config = ConfigDict(frozen=True)
        user_id: str

    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Frozen context", "path": ""}]}'
    context = ToolContext(
        context=FrozenRunContext(user_id="abc"),
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match="Frozen Pydantic models"):
        await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 0


@pytest.mark.asyncio
async def test_codex_tool_run_context_thread_id_rejects_frozen_dataclass_context() -> None:
    @dataclass(frozen=True)
    class FrozenRunContext:
        user_id: str

    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Frozen dataclass", "path": ""}]}'
    context = ToolContext(
        context=FrozenRunContext(user_id="abc"),
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match="Frozen dataclass contexts"):
        await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 0


@pytest.mark.asyncio
async def test_codex_tool_run_context_thread_id_rejects_slots_object_without_thread_field() -> None:
    class SlotsRunContext:
        __slots__ = ("user_id",)

        def __init__(self, user_id: str) -> None:
            self.user_id = user_id

    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "Slots context", "path": ""}]}'
    context = ToolContext(
        context=SlotsRunContext(user_id="abc"),
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match='support field "codex_thread_id"'):
        await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 0


@pytest.mark.asyncio
async def test_codex_tool_run_context_thread_id_rejects_non_writable_object_context() -> None:
    state = CodexMockState()
    state.events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "Codex done."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    tool = codex_tool(
        CodexToolOptions(
            codex=cast(Codex, FakeCodex(state)),
            use_run_context_thread_id=True,
            failure_error_function=None,
        )
    )
    input_json = '{"inputs": [{"type": "text", "text": "List context", "path": ""}]}'
    context: ToolContext[Any] = ToolContext(
        context=cast(Any, []),
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(UserError, match="use_run_context_thread_id=True"):
        await tool.on_invoke_tool(context, input_json)

    assert state.start_calls == 0
    assert state.resume_calls == 0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"type": "text", "text": "", "path": ""}, 'non-empty "text"'),
        ({"type": "text", "text": "hello", "path": "x"}, '"path" is not allowed'),
        ({"type": "local_image", "path": ""}, 'non-empty "path"'),
        ({"type": "local_image", "path": "img.png", "text": "hi"}, '"text" is not allowed'),
    ],
)
def test_codex_tool_input_item_validation_errors(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        codex_tool_module.CodexToolInputItem(**payload)


def test_codex_tool_result_stringifies() -> None:
    result = CodexToolResult(thread_id="thread-1", response="ok", usage=None)
    assert json.loads(str(result)) == result.as_dict()


def test_codex_tool_parse_input_rejects_invalid_json() -> None:
    with pytest.raises(ModelBehaviorError, match="Invalid JSON input for codex tool"):
        codex_tool_module._parse_tool_input(codex_tool_module.CodexToolParameters, "{bad")


def test_codex_tool_normalize_parameters_requires_inputs() -> None:
    class Dummy(BaseModel):
        model_config = ConfigDict(extra="forbid")

    with pytest.raises(UserError, match="must include an inputs field"):
        codex_tool_module._normalize_parameters(Dummy())


def test_codex_tool_coerce_options_rejects_unknown_fields() -> None:
    with pytest.raises(UserError, match="Unknown Codex tool option"):
        codex_tool_module._coerce_tool_options({"unknown": "value"})


def test_codex_tool_keyword_rejects_empty_run_context_key() -> None:
    with pytest.raises(UserError, match="run_context_thread_id_key"):
        codex_tool(run_context_thread_id_key=" ")


def test_codex_tool_resolve_output_schema_validation_errors() -> None:
    with pytest.raises(UserError, match="must include properties"):
        codex_tool_module._resolve_output_schema({"properties": []})
    with pytest.raises(UserError, match="Invalid schema for output property"):
        codex_tool_module._resolve_output_schema(
            {"properties": [{"name": "bad", "schema": {"type": "bogus"}}]}
        )
    with pytest.raises(UserError, match="Required property"):
        codex_tool_module._resolve_output_schema(
            {
                "properties": [{"name": "name", "schema": {"type": "string"}}],
                "required": ["missing"],
            }
        )
    with pytest.raises(UserError, match='type "object"'):
        codex_tool_module._resolve_output_schema({"type": "string"})


def test_codex_tool_resolve_output_schema_descriptor() -> None:
    descriptor = {
        "title": "Report",
        "description": "Structured output",
        "properties": [
            {
                "name": "tags",
                "description": "Tag list",
                "schema": {
                    "type": "array",
                    "description": "Tags array",
                    "items": {"type": "string", "description": "Tag value"},
                },
            },
            {
                "name": "summary",
                "description": "Summary text",
                "schema": {"type": "string"},
            },
        ],
        "required": ["tags"],
    }
    schema = codex_tool_module._resolve_output_schema(descriptor)
    assert schema["title"] == "Report"
    assert schema["description"] == "Structured output"
    assert schema["properties"]["tags"]["type"] == "array"
    assert schema["properties"]["tags"]["description"] == "Tag list"
    assert schema["properties"]["tags"]["items"]["description"] == "Tag value"
    assert schema["properties"]["tags"]["items"]["type"] == "string"
    assert schema["required"] == ["tags"]


def test_codex_tool_resolve_codex_options_reads_env_override() -> None:
    options = codex_tool_module.CodexOptions(
        codex_path_override="/bin/codex",
        env={"CODEX_API_KEY": "env-key"},
    )
    resolved = codex_tool_module._resolve_codex_options(options)
    assert resolved is not None
    assert resolved.api_key == "env-key"
    assert resolved.codex_path_override == "/bin/codex"


@pytest.mark.asyncio
async def test_codex_tool_create_codex_resolver_caches_instance() -> None:
    options = codex_tool_module.CodexOptions(codex_path_override="/bin/codex")
    resolver = codex_tool_module._create_codex_resolver(None, options)
    first = await resolver()
    second = await resolver()
    assert first is second


def test_codex_tool_resolve_thread_options_merges_values() -> None:
    resolved = codex_tool_module._resolve_thread_options(
        {"model": "gpt-4.1-mini"},
        sandbox_mode="read-only",
        working_directory="/work",
        skip_git_repo_check=True,
    )
    assert resolved is not None
    assert resolved.model == "gpt-4.1-mini"
    assert resolved.sandbox_mode == "read-only"
    assert resolved.working_directory == "/work"
    assert resolved.skip_git_repo_check is True


def test_codex_tool_resolve_thread_options_empty_is_none() -> None:
    assert codex_tool_module._resolve_thread_options(None, None, None, None) is None


def test_codex_tool_build_turn_options_merges_output_schema() -> None:
    output_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    turn = codex_tool_module._build_turn_options(None, output_schema)
    assert turn.output_schema == output_schema

    turn_defaults = codex_tool_module.TurnOptions(
        output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        idle_timeout_seconds=1.0,
    )
    turn = codex_tool_module._build_turn_options(turn_defaults, None)
    assert turn.output_schema == turn_defaults.output_schema
    assert turn.idle_timeout_seconds == 1.0


def test_codex_tool_persisted_thread_mismatch_raises() -> None:
    class DummyThread:
        def __init__(self, thread_id: str) -> None:
            self.id = thread_id

    with pytest.raises(UserError, match="already has an active thread"):
        codex_tool_module._get_or_create_persisted_thread(
            codex=object(),
            thread_id="thread-2",
            thread_options=None,
            existing_thread=DummyThread("thread-1"),
        )


def test_codex_tool_default_response_text() -> None:
    assert (
        codex_tool_module._build_default_response({"inputs": None})
        == "Codex task completed with no inputs."
    )


def test_codex_tool_input_item_accepts_local_image() -> None:
    item = codex_tool_module.CodexToolInputItem(type="local_image", path=" /tmp/img.png ")
    assert item.path == "/tmp/img.png"
    assert item.text is None


def test_codex_tool_normalize_parameters_handles_local_image() -> None:
    params = codex_tool_module.CodexToolParameters(
        inputs=[
            codex_tool_module.CodexToolInputItem(type="text", text="hello"),
            codex_tool_module.CodexToolInputItem(type="local_image", path="/tmp/img.png"),
        ]
    )
    normalized = codex_tool_module._normalize_parameters(params)
    assert normalized["inputs"] == [
        {"type": "text", "text": "hello"},
        {"type": "local_image", "path": "/tmp/img.png"},
    ]
    assert normalized["thread_id"] is None


def test_codex_tool_input_thread_id_validation_errors() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        codex_tool_module.CodexToolParameters(
            inputs=[codex_tool_module.CodexToolInputItem(type="text", text="hello")],
            thread_id="   ",
        )


def test_codex_tool_build_codex_input_empty() -> None:
    assert codex_tool_module._build_codex_input({"inputs": None}) == ""


def test_codex_tool_truncate_span_string_limits() -> None:
    assert codex_tool_module._truncate_span_string("hello", 0) == ""
    long_value = "x" * 100
    assert codex_tool_module._truncate_span_string(long_value, 3) == "xxx"


def test_codex_tool_truncate_span_value_handles_circular_reference() -> None:
    value: list[Any] = []
    value.append(value)
    truncated = codex_tool_module._truncate_span_value(value, 1)
    assert isinstance(truncated, dict)
    assert truncated["truncated"] is True


def test_codex_tool_enforce_span_data_budget_zero_max() -> None:
    assert codex_tool_module._enforce_span_data_budget({"output": "x"}, 0) == {}


def test_codex_tool_enforce_span_data_budget_trims_values_when_budget_tight() -> None:
    data = {"command": "run", "output": "x" * 50, "arguments": "y" * 50}
    base = {"command": "run", "output": "", "arguments": ""}
    max_chars = codex_tool_module._json_char_size(base) + 1
    trimmed = codex_tool_module._enforce_span_data_budget(data, max_chars)
    assert codex_tool_module._json_char_size(trimmed) <= max_chars
    assert "command" in trimmed
    assert "output" in trimmed
    assert "arguments" in trimmed


def test_codex_tool_enforce_span_data_budget_drops_until_base_fits() -> None:
    data = {"command": "run", "output": "x" * 50}
    base = {"command": "", "output": ""}
    max_chars = codex_tool_module._json_char_size(base) - 1
    trimmed = codex_tool_module._enforce_span_data_budget(data, max_chars)
    assert not ("command" in trimmed and "output" in trimmed)


def test_codex_tool_handle_item_started_ignores_missing_id() -> None:
    spans: dict[str, Any] = {}
    codex_tool_module._handle_item_started({"type": "reasoning", "text": "hi"}, spans, None)
    assert spans == {}


def test_codex_tool_handle_item_updated_ignores_missing_span() -> None:
    codex_tool_module._handle_item_updated(
        {"id": "missing", "type": "reasoning", "text": "hi"}, {}, None
    )


@pytest.mark.asyncio
async def test_codex_tool_on_invoke_tool_handles_failure_error_function_sync() -> None:
    def failure_error_function(_ctx: RunContextWrapper[Any], _exc: Exception) -> str:
        return "handled"

    tool = codex_tool(CodexToolOptions(failure_error_function=failure_error_function))
    input_json = "{bad"
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    result = await tool.on_invoke_tool(context, input_json)
    assert result == "handled"


@pytest.mark.asyncio
async def test_codex_tool_on_invoke_tool_handles_failure_error_function_async() -> None:
    async def failure_error_function(_ctx: RunContextWrapper[Any], _exc: Exception) -> str:
        return "handled-async"

    tool = codex_tool(CodexToolOptions(failure_error_function=failure_error_function))
    input_json = "{bad"
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    result = await tool.on_invoke_tool(context, input_json)
    assert result == "handled-async"


@pytest.mark.asyncio
async def test_codex_tool_on_invoke_tool_raises_without_failure_handler() -> None:
    tool = codex_tool(CodexToolOptions(failure_error_function=None))
    input_json = "{bad"
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=input_json,
    )

    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(context, input_json)


@pytest.mark.asyncio
async def test_codex_tool_consume_events_with_on_stream_error() -> None:
    events = [
        {
            "type": "item.started",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "server",
                "tool": "tool",
                "arguments": {"q": "x"},
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "server",
                "tool": "tool",
                "arguments": {"q": "x"},
                "status": "failed",
                "error": {"message": "boom"},
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "done"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]

    async def event_stream():
        for event in events:
            yield event

    callbacks: list[str] = []

    def on_stream(payload: CodexToolStreamEvent) -> None:
        callbacks.append(payload.event.type)
        if payload.event.type == "item.started":
            raise RuntimeError("boom")

    context = ToolContext(
        context=None,
        tool_name="codex",
        tool_call_id="call-1",
        tool_arguments="{}",
    )

    with trace("codex-test"):
        response, usage, thread_id = await codex_tool_module._consume_events(
            event_stream(),
            {"inputs": [{"type": "text", "text": "hello"}]},
            context,
            SimpleNamespace(id="thread-1"),
            on_stream,
            64,
        )

    assert response == "done"
    assert usage == Usage(input_tokens=1, cached_input_tokens=0, output_tokens=1)
    assert thread_id == "thread-1"
    assert "item.started" in callbacks


@pytest.mark.asyncio
async def test_codex_tool_consume_events_default_response() -> None:
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        }
    ]

    async def event_stream():
        for event in events:
            yield event

    context = ToolContext(
        context=None,
        tool_name="codex",
        tool_call_id="call-1",
        tool_arguments="{}",
    )

    response, usage, thread_id = await codex_tool_module._consume_events(
        event_stream(),
        {"inputs": [{"type": "text", "text": "hello"}]},
        context,
        SimpleNamespace(id="thread-1"),
        None,
        None,
    )

    assert response == "Codex task completed with inputs."
    assert usage == Usage(input_tokens=1, cached_input_tokens=0, output_tokens=1)
    assert thread_id == "thread-1"


@pytest.mark.asyncio
async def test_codex_tool_consume_events_turn_failed() -> None:
    events = [{"type": "turn.failed", "error": {"message": "boom"}}]

    async def event_stream():
        for event in events:
            yield event

    context = ToolContext(
        context=None,
        tool_name="codex",
        tool_call_id="call-1",
        tool_arguments="{}",
    )

    with pytest.raises(UserError, match="Codex turn failed: boom"):
        await codex_tool_module._consume_events(
            event_stream(),
            {"inputs": [{"type": "text", "text": "hello"}]},
            context,
            SimpleNamespace(id="thread-1"),
            None,
            None,
        )


@pytest.mark.asyncio
async def test_codex_tool_consume_events_error_event() -> None:
    events = [{"type": "error", "message": "boom"}]

    async def event_stream():
        for event in events:
            yield event

    context = ToolContext(
        context=None,
        tool_name="codex",
        tool_call_id="call-1",
        tool_arguments="{}",
    )

    with pytest.raises(UserError, match="Codex stream error"):
        await codex_tool_module._consume_events(
            event_stream(),
            {"inputs": [{"type": "text", "text": "hello"}]},
            context,
            SimpleNamespace(id="thread-1"),
            None,
            None,
        )


@pytest.mark.asyncio
async def test_codex_tool_create_codex_resolver_with_provided() -> None:
    state = CodexMockState()
    provided = cast(Codex, FakeCodex(state))
    resolver = codex_tool_module._create_codex_resolver(provided, None)
    resolved = await resolver()
    assert resolved is provided


def test_codex_tool_build_turn_options_overrides_schema() -> None:
    output_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    turn_defaults = codex_tool_module.TurnOptions(
        output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        idle_timeout_seconds=1.0,
    )
    turn = codex_tool_module._build_turn_options(turn_defaults, output_schema)
    assert turn.output_schema == output_schema


def test_codex_tool_resolve_codex_options_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "env-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resolved = codex_tool_module._resolve_codex_options(None)
    assert resolved is not None
    assert resolved.api_key == "env-key"


def test_codex_tool_accepts_all_keyword_overrides() -> None:
    state = CodexMockState()

    class CustomParams(BaseModel):
        inputs: list[CodexToolInputItem]

        model_config = ConfigDict(extra="forbid")

    tool = codex_tool(
        CodexToolOptions(codex=cast(Codex, FakeCodex(state))),
        name="codex_overrides",
        description="desc",
        parameters=CustomParams,
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        codex=cast(Codex, FakeCodex(state)),
        codex_options={"api_key": "from-kwargs"},
        default_thread_options={"model": "gpt"},
        thread_id="thread-1",
        sandbox_mode="read-only",
        working_directory="/work",
        skip_git_repo_check=True,
        default_turn_options={"idle_timeout_seconds": 1.0},
        span_data_max_chars=10,
        persist_session=True,
        on_stream=lambda _payload: None,
        is_enabled=False,
        failure_error_function=lambda _ctx, _exc: "handled",
        use_run_context_thread_id=True,
        run_context_thread_id_key="thread_key",
    )

    assert tool.name == "codex_overrides"


def test_codex_tool_coerce_options_rejects_empty_run_context_key() -> None:
    with pytest.raises(UserError, match="run_context_thread_id_key"):
        codex_tool_module._coerce_tool_options(
            {
                "use_run_context_thread_id": True,
                "run_context_thread_id_key": " ",
            }
        )
