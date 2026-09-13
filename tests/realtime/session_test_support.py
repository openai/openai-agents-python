"""Explicitly imported helpers and function-scoped fixtures for session tests."""

from typing import Any
from unittest.mock import AsyncMock, Mock, PropertyMock

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.testing import RealtimeConnectCall, ScriptedRealtimeModel
from agents.tool import FunctionTool, function_tool


class _DummyModel(ScriptedRealtimeModel):
    def __init__(self) -> None:
        super().__init__(strict=False)

    @property
    def events(self) -> tuple[Any, ...]:
        return self.sent_events

    @property
    def connect_options(self) -> RealtimeConnectCall | None:
        return self.connect_calls[-1] if self.connect_calls else None


class RecordingRealtimeModel(ScriptedRealtimeModel):
    def __init__(self):
        super().__init__(strict=False)
        # Legacy tracking for tests that haven't been updated yet
        self.sent_messages = []
        self.sent_audio = []
        self.sent_tool_outputs = []
        self.interrupts_called = 0
        self.retired_audio_response_ids = []

    async def send_event(self, event):
        from agents.realtime.model_inputs import (
            RealtimeModelSendAudio,
            RealtimeModelSendInterrupt,
            RealtimeModelSendToolOutput,
            RealtimeModelSendUserInput,
        )

        self._sent_events.append(self._snapshot_send_event(event))

        # Update legacy tracking for compatibility
        if isinstance(event, RealtimeModelSendUserInput):
            self.sent_messages.append(event.user_input)
        elif isinstance(event, RealtimeModelSendAudio):
            self.sent_audio.append((event.audio, event.commit))
        elif isinstance(event, RealtimeModelSendToolOutput):
            self.sent_tool_outputs.append((event.tool_call, event.output, event.start_response))
        elif isinstance(event, RealtimeModelSendInterrupt):
            self.interrupts_called += 1

    async def send_event_if(self, event, send_if):
        if not send_if():
            return False
        await self.send_event(event)
        return True

    def _retire_response_audio(self, response_id: str) -> None:
        self.retired_audio_response_ids.append(response_id)


@pytest.fixture
def mock_agent():
    agent = Mock(spec=RealtimeAgent)
    agent.get_all_tools = AsyncMock(return_value=[])

    type(agent).handoffs = PropertyMock(return_value=[])
    type(agent).output_guardrails = PropertyMock(return_value=[])
    return agent


@pytest.fixture
def mock_model():
    return RecordingRealtimeModel()


def _set_default_timeout_fields(tool: Mock) -> Mock:
    tool.timeout_seconds = None
    tool.timeout_behavior = "error_as_result"
    tool.timeout_error_function = None
    return tool


def _named_function_tool(
    name: str,
    output: str,
    *,
    needs_approval: bool = False,
) -> FunctionTool:
    def tool_func() -> str:
        return output

    tool = function_tool(tool_func, name_override=name)
    tool.needs_approval = needs_approval
    return tool


def _sent_tool_output_strings(model: RecordingRealtimeModel) -> list[str]:
    return [output for _call, output, _start_response in model.sent_tool_outputs]


@pytest.fixture
def mock_function_tool():
    tool = _set_default_timeout_fields(Mock(spec=FunctionTool))
    tool.name = "test_function"
    tool.on_invoke_tool = AsyncMock(return_value="function_result")
    tool.needs_approval = False
    return tool
