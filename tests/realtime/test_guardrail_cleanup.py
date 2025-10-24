"""Test guardrail and tool call task cleanup to ensure proper exception handling.

This test verifies the fix for bugs where _cleanup_guardrail_tasks() and
_cleanup_tool_call_tasks() were not properly awaiting cancelled tasks, which could
lead to unhandled task exceptions and potential memory leaks.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, PropertyMock

import pytest

from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail
from agents.realtime import RealtimeSession
from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig
from agents.realtime.model import RealtimeModel
from agents.realtime.model_events import (
    RealtimeModelToolCallEvent,
    RealtimeModelTranscriptDeltaEvent,
)
from agents.tool import FunctionTool


class MockRealtimeModel(RealtimeModel):
    """Mock realtime model for testing."""

    def __init__(self):
        super().__init__()
        self.listeners = []
        self.connect_called = False
        self.close_called = False
        self.sent_events = []
        self.sent_messages = []
        self.sent_audio = []
        self.sent_tool_outputs = []
        self.interrupts_called = 0

    async def connect(self, options=None):
        self.connect_called = True

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def send_event(self, event):
        from agents.realtime.model_inputs import (
            RealtimeModelSendAudio,
            RealtimeModelSendInterrupt,
            RealtimeModelSendToolOutput,
            RealtimeModelSendUserInput,
        )

        self.sent_events.append(event)

        # Update legacy tracking for compatibility
        if isinstance(event, RealtimeModelSendUserInput):
            self.sent_messages.append(event.user_input)
        elif isinstance(event, RealtimeModelSendAudio):
            self.sent_audio.append((event.audio, event.commit))
        elif isinstance(event, RealtimeModelSendToolOutput):
            self.sent_tool_outputs.append((event.tool_call, event.output, event.start_response))
        elif isinstance(event, RealtimeModelSendInterrupt):
            self.interrupts_called += 1

    async def close(self):
        self.close_called = True


@pytest.fixture
def mock_model():
    return MockRealtimeModel()


@pytest.fixture
def mock_agent():
    agent = Mock(spec=RealtimeAgent)
    agent.name = "test_agent"
    agent.get_all_tools = AsyncMock(return_value=[])
    type(agent).handoffs = PropertyMock(return_value=[])
    type(agent).output_guardrails = PropertyMock(return_value=[])
    return agent


@pytest.mark.asyncio
async def test_guardrail_task_cleanup_awaits_cancelled_tasks(mock_model, mock_agent):
    """Test that cleanup properly awaits cancelled guardrail tasks.

    This test verifies that when guardrail tasks are cancelled during cleanup,
    the cleanup method properly awaits them to completion using asyncio.gather()
    with return_exceptions=True. This ensures:
    1. No warnings about unhandled task exceptions
    2. Proper resource cleanup
    3. No memory leaks from abandoned tasks
    """

    # Create a guardrail that runs a long async operation
    task_started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def slow_guardrail_func(context, agent, output):
        """A guardrail that takes time to execute."""
        task_started.set()
        try:
            # Simulate a long-running operation
            await asyncio.sleep(10)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    guardrail = OutputGuardrail(guardrail_function=slow_guardrail_func, name="slow_guardrail")

    run_config: RealtimeRunConfig = {
        "output_guardrails": [guardrail],
        "guardrails_settings": {"debounce_text_length": 5},
    }

    session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

    # Trigger a guardrail by sending a transcript delta
    transcript_event = RealtimeModelTranscriptDeltaEvent(
        item_id="item_1", delta="hello world", response_id="resp_1"
    )

    await session.on_event(transcript_event)

    # Wait for the guardrail task to start
    await asyncio.wait_for(task_started.wait(), timeout=1.0)

    # Verify a guardrail task was created
    assert len(session._guardrail_tasks) == 1
    task = list(session._guardrail_tasks)[0]
    assert not task.done()

    # Now cleanup the session - this should cancel and await the task
    await session._cleanup_guardrail_tasks()

    # Verify the task was cancelled and properly awaited
    assert task_cancelled.is_set(), "Task should have received CancelledError"
    assert len(session._guardrail_tasks) == 0, "Tasks list should be cleared"

    # No warnings should be raised about unhandled task exceptions


@pytest.mark.asyncio
async def test_guardrail_task_cleanup_with_exception(mock_model, mock_agent):
    """Test that cleanup handles guardrail tasks that raise exceptions.

    This test verifies that if a guardrail task raises an exception (not just
    CancelledError), the cleanup method still completes successfully and doesn't
    propagate the exception, thanks to return_exceptions=True.
    """

    task_started = asyncio.Event()
    exception_raised = asyncio.Event()

    async def failing_guardrail_func(context, agent, output):
        """A guardrail that raises an exception."""
        task_started.set()
        try:
            await asyncio.sleep(10)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)
        except asyncio.CancelledError as e:
            exception_raised.set()
            # Simulate an error during cleanup
            raise RuntimeError("Cleanup error") from e

    guardrail = OutputGuardrail(guardrail_function=failing_guardrail_func, name="failing_guardrail")

    run_config: RealtimeRunConfig = {
        "output_guardrails": [guardrail],
        "guardrails_settings": {"debounce_text_length": 5},
    }

    session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

    # Trigger a guardrail
    transcript_event = RealtimeModelTranscriptDeltaEvent(
        item_id="item_1", delta="hello world", response_id="resp_1"
    )

    await session.on_event(transcript_event)

    # Wait for the guardrail task to start
    await asyncio.wait_for(task_started.wait(), timeout=1.0)

    # Cleanup should not raise the RuntimeError due to return_exceptions=True
    await session._cleanup_guardrail_tasks()

    # Verify cleanup completed successfully
    assert exception_raised.is_set()
    assert len(session._guardrail_tasks) == 0


@pytest.mark.asyncio
async def test_guardrail_task_cleanup_with_multiple_tasks(mock_model, mock_agent):
    """Test cleanup with multiple pending guardrail tasks.

    This test verifies that cleanup properly handles multiple concurrent guardrail
    tasks by triggering guardrails multiple times, then cancelling and awaiting all of them.
    """

    tasks_started = asyncio.Event()
    tasks_cancelled = 0

    async def slow_guardrail_func(context, agent, output):
        nonlocal tasks_cancelled
        tasks_started.set()
        try:
            await asyncio.sleep(10)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)
        except asyncio.CancelledError:
            tasks_cancelled += 1
            raise

    guardrail = OutputGuardrail(guardrail_function=slow_guardrail_func, name="slow_guardrail")

    run_config: RealtimeRunConfig = {
        "output_guardrails": [guardrail],
        "guardrails_settings": {"debounce_text_length": 5},
    }

    session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

    # Trigger guardrails multiple times to create multiple tasks
    for i in range(3):
        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id=f"item_{i}", delta="hello world", response_id=f"resp_{i}"
        )
        await session.on_event(transcript_event)

    # Wait for at least one task to start
    await asyncio.wait_for(tasks_started.wait(), timeout=1.0)

    # Should have at least one guardrail task
    initial_task_count = len(session._guardrail_tasks)
    assert initial_task_count >= 1, "At least one guardrail task should exist"

    # Cleanup should cancel and await all tasks
    await session._cleanup_guardrail_tasks()

    # Verify all tasks were cancelled and cleared
    assert tasks_cancelled >= 1, "At least one task should have been cancelled"
    assert len(session._guardrail_tasks) == 0


@pytest.mark.asyncio
async def test_tool_call_task_cleanup_awaits_cancelled_tasks(mock_model, mock_agent):
    """Test that cleanup properly awaits cancelled tool call tasks.

    This test verifies that when tool call tasks are cancelled during cleanup,
    the cleanup method properly awaits them to completion using asyncio.gather()
    with return_exceptions=True.
    """
    task_started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def slow_tool_handler(context, tool_call):
        """A tool handler that takes time to execute."""
        task_started.set()
        try:
            await asyncio.sleep(10)
            return "result"
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    # Mock the tool
    mock_tool = Mock(spec=FunctionTool)
    mock_tool.name = "slow_tool"
    mock_tool.on_invoke_tool = slow_tool_handler

    mock_agent.get_all_tools = AsyncMock(return_value=[mock_tool])

    run_config: RealtimeRunConfig = {}
    session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

    # Trigger a tool call
    tool_call_event = RealtimeModelToolCallEvent(
        name="slow_tool",
        call_id="call_1",
        arguments='{"arg": "test"}',
    )

    await session.on_event(tool_call_event)

    # Wait for the tool call task to start
    await asyncio.wait_for(task_started.wait(), timeout=1.0)

    # Verify a tool call task was created
    assert len(session._tool_call_tasks) == 1
    task = list(session._tool_call_tasks)[0]
    assert not task.done()

    # Now cleanup the session - this should cancel and await the task
    await session._cleanup_tool_call_tasks()

    # Verify the task was cancelled and properly awaited
    assert task_cancelled.is_set(), "Task should have received CancelledError"
    assert len(session._tool_call_tasks) == 0, "Tasks list should be cleared"


@pytest.mark.asyncio
async def test_tool_call_task_cleanup_with_exception(mock_model, mock_agent):
    """Test that cleanup handles tool call tasks that raise exceptions."""
    task_started = asyncio.Event()
    exception_raised = asyncio.Event()

    async def failing_tool_handler(context, tool_call):
        """A tool handler that raises an exception."""
        task_started.set()
        try:
            await asyncio.sleep(10)
            return "result"
        except asyncio.CancelledError as e:
            exception_raised.set()
            raise RuntimeError("Tool cleanup error") from e

    # Mock the tool
    mock_tool = Mock(spec=FunctionTool)
    mock_tool.name = "failing_tool"
    mock_tool.on_invoke_tool = failing_tool_handler

    mock_agent.get_all_tools = AsyncMock(return_value=[mock_tool])

    run_config: RealtimeRunConfig = {}
    session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

    # Trigger a tool call
    tool_call_event = RealtimeModelToolCallEvent(
        name="failing_tool",
        call_id="call_1",
        arguments='{"arg": "test"}',
    )

    await session.on_event(tool_call_event)

    # Wait for the tool call task to start
    await asyncio.wait_for(task_started.wait(), timeout=1.0)

    # Cleanup should not raise the RuntimeError due to return_exceptions=True
    await session._cleanup_tool_call_tasks()

    # Verify cleanup completed successfully
    assert exception_raised.is_set()
    assert len(session._tool_call_tasks) == 0


@pytest.mark.asyncio
async def test_cleanup_method_awaits_both_task_types(mock_model, mock_agent):
    """Test that _cleanup() properly awaits both guardrail and tool call tasks."""
    guardrail_cancelled = asyncio.Event()
    tool_call_cancelled = asyncio.Event()

    async def slow_guardrail_func(context, agent, output):
        try:
            await asyncio.sleep(10)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)
        except asyncio.CancelledError:
            guardrail_cancelled.set()
            raise

    async def slow_tool_handler(context, tool_call):
        try:
            await asyncio.sleep(10)
            return "result"
        except asyncio.CancelledError:
            tool_call_cancelled.set()
            raise

    guardrail = OutputGuardrail(guardrail_function=slow_guardrail_func, name="slow_guardrail")

    # Mock the tool
    mock_tool = Mock(spec=FunctionTool)
    mock_tool.name = "slow_tool"
    mock_tool.on_invoke_tool = slow_tool_handler

    mock_agent.get_all_tools = AsyncMock(return_value=[mock_tool])

    run_config: RealtimeRunConfig = {
        "output_guardrails": [guardrail],
        "guardrails_settings": {"debounce_text_length": 5},
    }

    session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

    # Trigger both a guardrail and a tool call
    transcript_event = RealtimeModelTranscriptDeltaEvent(
        item_id="item_1", delta="hello world", response_id="resp_1"
    )
    await session.on_event(transcript_event)

    tool_call_event = RealtimeModelToolCallEvent(
        name="slow_tool",
        call_id="call_1",
        arguments='{"arg": "test"}',
    )
    await session.on_event(tool_call_event)

    # Give tasks time to start
    await asyncio.sleep(0.1)

    # Verify tasks were created
    assert len(session._guardrail_tasks) >= 1
    assert len(session._tool_call_tasks) >= 1

    # Call _cleanup() which should await both cleanup methods
    await session._cleanup()

    # Verify both task types were cancelled
    assert guardrail_cancelled.is_set(), "Guardrail task should be cancelled"
    assert tool_call_cancelled.is_set(), "Tool call task should be cancelled"
    assert len(session._guardrail_tasks) == 0
    assert len(session._tool_call_tasks) == 0
