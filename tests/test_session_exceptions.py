from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import websockets.exceptions

from agents.realtime.events import RealtimeError
from agents.realtime.model import RealtimeModel, RealtimeModelConfig, RealtimeModelListener
from agents.realtime.model_events import (
    RealtimeModelErrorEvent,
    RealtimeModelEvent,
    RealtimeModelExceptionEvent,
)
from agents.realtime.session import RealtimeSession


class FakeRealtimeModel(RealtimeModel):
    """Fake model for testing that forwards events to listeners."""

    def __init__(self):
        self._listeners: list[RealtimeModelListener] = []
        self._events_to_send: list[RealtimeModelEvent] = []
        self._is_connected = False
        self._send_task: asyncio.Task[None] | None = None

    def set_next_events(self, events: list[RealtimeModelEvent]) -> None:
        """Set events to be sent to listeners."""
        self._events_to_send = events.copy()

    async def connect(self, options: RealtimeModelConfig) -> None:
        """Fake connection that starts sending events."""
        self._is_connected = True
        self._send_task = asyncio.create_task(self._send_events())

    async def _send_events(self) -> None:
        """Send queued events to all listeners."""
        for event in self._events_to_send:
            await asyncio.sleep(0.001)  # Small delay to simulate async behavior
            for listener in self._listeners:
                await listener.on_event(event)

    def add_listener(self, listener: RealtimeModelListener) -> None:
        """Add a listener."""
        self._listeners.append(listener)

    def remove_listener(self, listener: RealtimeModelListener) -> None:
        """Remove a listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def close(self) -> None:
        """Close the fake model."""
        self._is_connected = False
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass

    async def send_message(
        self, message: Any, other_event_data: dict[str, Any] | None = None
    ) -> None:
        """Fake send message."""
        pass

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Fake send audio."""
        pass

    async def send_event(self, event: Any) -> None:
        """Fake send event."""
        pass

    async def send_tool_output(self, tool_call: Any, output: str, start_response: bool) -> None:
        """Fake send tool output."""
        pass

    async def interrupt(self) -> None:
        """Fake interrupt."""
        pass


@pytest.fixture
def fake_agent():
    """Create a fake agent for testing."""
    agent = Mock()
    agent.get_all_tools = AsyncMock(return_value=[])
    return agent


@pytest.fixture
def fake_model():
    """Create a fake model for testing."""
    return FakeRealtimeModel()


class TestSessionExceptions:
    """Test exception handling in RealtimeSession."""

    @pytest.mark.asyncio
    async def test_end_to_end_exception_propagation_and_cleanup(
        self, fake_model: FakeRealtimeModel, fake_agent
    ):
        """Test that exceptions are stored, trigger cleanup, and are raised in __aiter__."""
        # Create test exception
        test_exception = ValueError("Test error")
        exception_event = RealtimeModelExceptionEvent(
            exception=test_exception, context="Test context"
        )

        # Set up session
        session = RealtimeSession(fake_model, fake_agent, None)

        # Set events to send
        fake_model.set_next_events([exception_event])

        # Start session
        async with session:
            # Try to iterate and expect exception
            with pytest.raises(ValueError, match="Test error"):
                async for _ in session:
                    pass  # Should never reach here

        # Verify cleanup occurred
        assert session._closed is True
        assert session._stored_exception == test_exception
        assert fake_model._is_connected is False
        assert len(fake_model._listeners) == 0

    @pytest.mark.asyncio
    async def test_websocket_connection_closure_type_distinction(
        self, fake_model: FakeRealtimeModel, fake_agent
    ):
        """Test different WebSocket closure types generate appropriate events."""
        # Test ConnectionClosed (should create exception event)
        error_closure = websockets.exceptions.ConnectionClosed(None, None)
        error_event = RealtimeModelExceptionEvent(
            exception=error_closure, context="WebSocket connection closed unexpectedly"
        )

        session = RealtimeSession(fake_model, fake_agent, None)
        fake_model.set_next_events([error_event])

        with pytest.raises(websockets.exceptions.ConnectionClosed):
            async with session:
                async for _event in session:
                    pass

        # Verify error closure triggered cleanup
        assert session._closed is True
        assert isinstance(session._stored_exception, websockets.exceptions.ConnectionClosed)

    @pytest.mark.asyncio
    async def test_json_parsing_error_handling(self, fake_model: FakeRealtimeModel, fake_agent):
        """Test JSON parsing errors are properly handled and contextualized."""
        # Create JSON decode error
        json_error = json.JSONDecodeError("Invalid JSON", "bad json", 0)
        json_exception_event = RealtimeModelExceptionEvent(
            exception=json_error, context="Failed to parse WebSocket message as JSON"
        )

        session = RealtimeSession(fake_model, fake_agent, None)
        fake_model.set_next_events([json_exception_event])

        with pytest.raises(json.JSONDecodeError):
            async with session:
                async for _event in session:
                    pass

        # Verify context is preserved
        assert session._stored_exception == json_error
        assert session._closed is True

    @pytest.mark.asyncio
    async def test_exception_context_preservation(self, fake_model: FakeRealtimeModel, fake_agent):
        """Test that exception context information is preserved through the handling process."""
        test_contexts = [
            ("Failed to send audio", RuntimeError("Audio encoding failed")),
            ("WebSocket error in message listener", ConnectionError("Network error")),
            ("Failed to send event: response.create", OSError("Socket closed")),
        ]

        for context, exception in test_contexts:
            exception_event = RealtimeModelExceptionEvent(exception=exception, context=context)

            session = RealtimeSession(fake_model, fake_agent, None)
            fake_model.set_next_events([exception_event])

            with pytest.raises(type(exception)):
                async with session:
                    async for _event in session:
                        pass

            # Verify the exact exception is stored
            assert session._stored_exception == exception
            assert session._closed is True

            # Reset for next iteration
            fake_model._is_connected = False
            fake_model._listeners.clear()

    @pytest.mark.asyncio
    async def test_multiple_exception_handling_behavior(
        self, fake_model: FakeRealtimeModel, fake_agent
    ):
        """Test behavior when multiple exceptions occur before consumption."""
        # Create multiple exceptions
        first_exception = ValueError("First error")
        second_exception = RuntimeError("Second error")

        first_event = RealtimeModelExceptionEvent(
            exception=first_exception, context="First context"
        )
        second_event = RealtimeModelExceptionEvent(
            exception=second_exception, context="Second context"
        )

        session = RealtimeSession(fake_model, fake_agent, None)
        fake_model.set_next_events([first_event, second_event])

        # Start session and let events process
        async with session:
            # Give time for events to be processed
            await asyncio.sleep(0.05)

        # The first exception should be stored (second should overwrite, but that's
        # the current behavior). In practice, once an exception occurs, cleanup
        # should prevent further processing
        assert session._stored_exception is not None
        assert session._closed is True

    @pytest.mark.asyncio
    async def test_exception_during_guardrail_processing(
        self, fake_model: FakeRealtimeModel, fake_agent
    ):
        """Test that exceptions don't interfere with guardrail task cleanup."""
        # Create exception event
        test_exception = RuntimeError("Processing error")
        exception_event = RealtimeModelExceptionEvent(
            exception=test_exception, context="Processing failed"
        )

        session = RealtimeSession(fake_model, fake_agent, None)

        # Add some fake guardrail tasks
        fake_task1 = Mock()
        fake_task1.done.return_value = False
        fake_task1.cancel = Mock()

        fake_task2 = Mock()
        fake_task2.done.return_value = True
        fake_task2.cancel = Mock()

        session._guardrail_tasks = {fake_task1, fake_task2}

        fake_model.set_next_events([exception_event])

        with pytest.raises(RuntimeError, match="Processing error"):
            async with session:
                async for _event in session:
                    pass

        # Verify guardrail tasks were properly cleaned up
        fake_task1.cancel.assert_called_once()
        fake_task2.cancel.assert_not_called()  # Already done
        assert len(session._guardrail_tasks) == 0

    @pytest.mark.asyncio
    async def test_normal_events_still_work_before_exception(
        self, fake_model: FakeRealtimeModel, fake_agent
    ):
        """Test that normal events are processed before an exception occurs."""
        # Create normal event followed by exception
        normal_event = RealtimeModelErrorEvent(error={"message": "Normal error"})
        exception_event = RealtimeModelExceptionEvent(
            exception=ValueError("Fatal error"), context="Fatal context"
        )

        session = RealtimeSession(fake_model, fake_agent, None)
        fake_model.set_next_events([normal_event, exception_event])

        events_received = []

        with pytest.raises(ValueError, match="Fatal error"):
            async with session:
                async for event in session:
                    events_received.append(event)

        # Should have received events before exception
        assert len(events_received) >= 1
        # Look for the error event (might not be first due to history_updated
        # being emitted initially)
        error_events = [e for e in events_received if hasattr(e, "type") and e.type == "error"]
        assert len(error_events) >= 1
        assert isinstance(error_events[0], RealtimeError)
