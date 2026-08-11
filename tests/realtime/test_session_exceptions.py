from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
import websockets.exceptions

from agents.realtime.events import RealtimeError
from agents.realtime.model_events import (
    RealtimeModelErrorEvent,
    RealtimeModelEvent,
    RealtimeModelExceptionEvent,
)
from agents.realtime.session import RealtimeSession
from agents.realtime.testing import ScriptedRealtimeModel


def model_with_events(*events: RealtimeModelEvent) -> ScriptedRealtimeModel:
    return ScriptedRealtimeModel(connect_events=events, strict=False)


@pytest.fixture
def fake_agent():
    """Create a fake agent for testing."""
    agent = Mock()
    agent.get_all_tools = AsyncMock(return_value=[])
    agent.get_system_prompt = AsyncMock(return_value="test instructions")
    agent.handoffs = []
    return agent


class TestSessionExceptions:
    """Test exception handling in RealtimeSession."""

    @pytest.mark.asyncio
    async def test_end_to_end_exception_propagation_and_cleanup(self, fake_agent):
        """Test that exceptions are stored, trigger cleanup, and are raised in __aiter__."""
        # Create test exception
        test_exception = ValueError("Test error")
        exception_event = RealtimeModelExceptionEvent(
            exception=test_exception, context="Test context"
        )

        # Set up session
        model = model_with_events(exception_event)
        session = RealtimeSession(model, fake_agent, None)

        # Start session
        async with session:
            # Try to iterate and expect exception
            with pytest.raises(ValueError, match="Test error"):
                async for _ in session:
                    pass  # Should never reach here

        # Verify cleanup occurred
        assert session._closed is True
        assert session._stored_exception == test_exception
        assert model.connected is False
        assert model.listeners == ()

    @pytest.mark.asyncio
    async def test_websocket_connection_closure_type_distinction(self, fake_agent):
        """Test different WebSocket closure types generate appropriate events."""
        # Test ConnectionClosed (should create exception event)
        error_closure = websockets.exceptions.ConnectionClosed(None, None)
        error_event = RealtimeModelExceptionEvent(
            exception=error_closure, context="WebSocket connection closed unexpectedly"
        )

        session = RealtimeSession(model_with_events(error_event), fake_agent, None)

        with pytest.raises(websockets.exceptions.ConnectionClosed):
            async with session:
                async for _event in session:
                    pass

        # Verify error closure triggered cleanup
        assert session._closed is True
        assert isinstance(session._stored_exception, websockets.exceptions.ConnectionClosed)

    @pytest.mark.asyncio
    async def test_json_parsing_error_handling(self, fake_agent):
        """Test JSON parsing errors are properly handled and contextualized."""
        # Create JSON decode error
        json_error = json.JSONDecodeError("Invalid JSON", "bad json", 0)
        json_exception_event = RealtimeModelExceptionEvent(
            exception=json_error, context="Failed to parse WebSocket message as JSON"
        )

        session = RealtimeSession(model_with_events(json_exception_event), fake_agent, None)

        with pytest.raises(json.JSONDecodeError):
            async with session:
                async for _event in session:
                    pass

        # Verify context is preserved
        assert session._stored_exception == json_error
        assert session._closed is True

    @pytest.mark.asyncio
    async def test_exception_context_preservation(self, fake_agent):
        """Test that exception context information is preserved through the handling process."""
        test_contexts = [
            ("Failed to send audio", RuntimeError("Audio encoding failed")),
            ("WebSocket error in message listener", ConnectionError("Network error")),
            ("Failed to send event: response.create", OSError("Socket closed")),
        ]

        for context, exception in test_contexts:
            exception_event = RealtimeModelExceptionEvent(exception=exception, context=context)

            model = model_with_events(exception_event)
            session = RealtimeSession(model, fake_agent, None)

            with pytest.raises(type(exception)):
                async with session:
                    async for _event in session:
                        pass

            # Verify the exact exception is stored
            assert session._stored_exception == exception
            assert session._closed is True

    @pytest.mark.asyncio
    async def test_multiple_exception_handling_behavior(self, fake_agent):
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

        session = RealtimeSession(model_with_events(first_event, second_event), fake_agent, None)

        # Start the session after both events are configured for connection.
        async with session:
            pass

        # The first exception should be stored (second should overwrite, but that's
        # the current behavior). In practice, once an exception occurs, cleanup
        # should prevent further processing
        assert session._stored_exception is not None
        assert session._closed is True

    @pytest.mark.asyncio
    async def test_exception_during_guardrail_processing(self, fake_agent):
        """Test that exceptions don't interfere with guardrail task cleanup."""
        # Create exception event
        test_exception = RuntimeError("Processing error")
        exception_event = RealtimeModelExceptionEvent(
            exception=test_exception, context="Processing failed"
        )

        model = model_with_events(exception_event)
        session = RealtimeSession(model, fake_agent, None)

        async def running_task() -> None:
            await asyncio.Event().wait()

        async def completed_task() -> None:
            return None

        pending = asyncio.create_task(running_task())
        completed = asyncio.create_task(completed_task())
        await asyncio.sleep(0)
        await completed
        session._guardrail_tasks = {pending, completed}

        with pytest.raises(RuntimeError, match="Processing error"):
            async with session:
                async for _event in session:
                    pass

        # Verify guardrail tasks were properly cleaned up.
        assert pending.cancelled()
        assert completed.done()
        assert not completed.cancelled()
        assert len(session._guardrail_tasks) == 0

    @pytest.mark.asyncio
    async def test_normal_events_still_work_before_exception(self, fake_agent):
        """Test that normal events are processed before an exception occurs."""
        # Create normal event followed by exception
        normal_event = RealtimeModelErrorEvent(error={"message": "Normal error"})
        exception_event = RealtimeModelExceptionEvent(
            exception=ValueError("Fatal error"), context="Fatal context"
        )

        model = ScriptedRealtimeModel(strict=False)
        session = RealtimeSession(model, fake_agent, None)

        events_received = []

        with pytest.raises(ValueError, match="Fatal error"):
            async with session:

                async def emit_events() -> None:
                    await model.emit(normal_event)
                    await asyncio.sleep(0)
                    await model.emit(exception_event)

                emitter = asyncio.create_task(emit_events())
                try:
                    async for event in session:
                        events_received.append(event)
                finally:
                    await emitter

        # Should have received events before exception
        assert len(events_received) >= 1
        # Look for the error event (might not be first due to history_updated
        # being emitted initially)
        error_events = [e for e in events_received if hasattr(e, "type") and e.type == "error"]
        assert len(error_events) >= 1
        assert isinstance(error_events[0], RealtimeError)
