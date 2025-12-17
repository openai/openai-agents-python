from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from openai.types.realtime.realtime_session_create_request import (
    RealtimeSessionCreateRequest,
)
from openai.types.realtime.realtime_tracing_config import TracingConfiguration

from agents.realtime.agent import RealtimeAgent
from agents.realtime.model import RealtimeModel
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel
from agents.realtime.session import RealtimeSession


class TestRealtimeTracingIntegration:
    """Test tracing configuration and session.update integration."""

    @pytest.fixture
    def model(self):
        """Create a fresh model instance for each test."""
        return OpenAIRealtimeWebSocketModel()

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock websocket connection."""
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()
        return mock_ws

    @pytest.mark.asyncio
    async def test_tracing_config_storage_and_defaults(self, model, mock_websocket):
        """Test that tracing config is stored correctly and defaults to 'auto'."""
        # Test with explicit tracing config
        config_with_tracing = {
            "api_key": "test-key",
            "initial_model_settings": {
                "tracing": {
                    "workflow_name": "test_workflow",
                    "group_id": "group_123",
                    "metadata": {"version": "1.0"},
                }
            },
        }

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()
                mock_create_task.return_value = mock_task
                mock_create_task.side_effect = lambda coro: (coro.close(), mock_task)[1]

                await model.connect(config_with_tracing)

                # Should store the tracing config
                assert model._tracing_config == {
                    "workflow_name": "test_workflow",
                    "group_id": "group_123",
                    "metadata": {"version": "1.0"},
                }

        # Test without tracing config - should default to "auto"
        model2 = OpenAIRealtimeWebSocketModel()
        config_no_tracing = {
            "api_key": "test-key",
            "initial_model_settings": {},
        }

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_create_task.side_effect = lambda coro: (coro.close(), mock_task)[1]

                await model2.connect(config_no_tracing)  # type: ignore[arg-type]
                assert model2._tracing_config == "auto"

    @pytest.mark.asyncio
    async def test_send_tracing_config_on_session_created(self, model, mock_websocket):
        """Test that tracing config is sent when session.created event is received."""
        config = {
            "api_key": "test-key",
            "initial_model_settings": {
                "tracing": {"workflow_name": "test_workflow", "group_id": "group_123"}
            },
        }

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()
                mock_create_task.side_effect = lambda coro: (coro.close(), mock_task)[1]

                await model.connect(config)

                # Simulate session.created event
                session_created_event = {
                    "type": "session.created",
                    "event_id": "event_123",
                    "session": {"id": "session_456", "type": "realtime", "model": "gpt-realtime"},
                }

                with patch.object(model, "_send_raw_message") as mock_send_raw_message:
                    await model._handle_ws_event(session_created_event)

                    # Should send session.update with tracing config
                    from openai.types.realtime.session_update_event import (
                        SessionUpdateEvent,
                    )

                    mock_send_raw_message.assert_called_once()
                    call_args = mock_send_raw_message.call_args[0][0]
                    assert isinstance(call_args, SessionUpdateEvent)
                    assert call_args.type == "session.update"
                    session_req = cast(RealtimeSessionCreateRequest, call_args.session)
                    assert isinstance(session_req.tracing, TracingConfiguration)
                    assert session_req.tracing.workflow_name == "test_workflow"
                    assert session_req.tracing.group_id == "group_123"

    @pytest.mark.asyncio
    async def test_send_tracing_config_auto_mode(self, model, mock_websocket):
        """Test that 'auto' tracing config is sent correctly."""
        config = {
            "api_key": "test-key",
            "initial_model_settings": {},  # No tracing config - defaults to "auto"
        }

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()
                mock_create_task.side_effect = lambda coro: (coro.close(), mock_task)[1]

                await model.connect(config)

                session_created_event = {
                    "type": "session.created",
                    "event_id": "event_123",
                    "session": {"id": "session_456", "type": "realtime", "model": "gpt-realtime"},
                }

                with patch.object(model, "_send_raw_message") as mock_send_raw_message:
                    await model._handle_ws_event(session_created_event)

                    # Should send session.update with "auto"
                    from openai.types.realtime.session_update_event import SessionUpdateEvent

                    mock_send_raw_message.assert_called_once()
                    call_args = mock_send_raw_message.call_args[0][0]
                    assert isinstance(call_args, SessionUpdateEvent)
                    assert call_args.type == "session.update"
                    session_req = cast(RealtimeSessionCreateRequest, call_args.session)
                    assert session_req.tracing == "auto"

    @pytest.mark.asyncio
    async def test_tracing_config_none_skips_session_update(self, model, mock_websocket):
        """Test that None tracing config skips sending session.update."""
        # Manually set tracing config to None (this would happen if explicitly set)
        model._tracing_config = None

        session_created_event = {
            "type": "session.created",
            "event_id": "event_123",
            "session": {"id": "session_456", "type": "realtime", "model": "gpt-realtime"},
        }

        with patch.object(model, "send_event") as mock_send_event:
            await model._handle_ws_event(session_created_event)

            # Should not send any session.update
            mock_send_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_tracing_config_with_metadata_serialization(self, model, mock_websocket):
        """Test that complex metadata in tracing config is handled correctly."""
        complex_metadata = {
            "user_id": "user_123",
            "session_type": "demo",
            "features": ["audio", "tools"],
            "config": {"timeout": 30, "retries": 3},
        }

        config = {
            "api_key": "test-key",
            "initial_model_settings": {
                "tracing": {"workflow_name": "complex_workflow", "metadata": complex_metadata}
            },
        }

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()
                mock_create_task.side_effect = lambda coro: (coro.close(), mock_task)[1]

                await model.connect(config)

                session_created_event = {
                    "type": "session.created",
                    "event_id": "event_123",
                    "session": {"id": "session_456", "type": "realtime", "model": "gpt-realtime"},
                }

                with patch.object(model, "_send_raw_message") as mock_send_raw_message:
                    await model._handle_ws_event(session_created_event)

                    # Should send session.update with complete tracing config including metadata
                    from openai.types.realtime.session_update_event import (
                        SessionUpdateEvent,
                    )

                    mock_send_raw_message.assert_called_once()
                    call_args = mock_send_raw_message.call_args[0][0]
                    assert isinstance(call_args, SessionUpdateEvent)
                    assert call_args.type == "session.update"
                    session_req = cast(RealtimeSessionCreateRequest, call_args.session)
                    assert isinstance(session_req.tracing, TracingConfiguration)
                    assert session_req.tracing.workflow_name == "complex_workflow"
                    assert session_req.tracing.metadata == complex_metadata

    @pytest.mark.asyncio
    async def test_tracing_disabled_prevents_tracing(self, mock_websocket):
        """Test that tracing_disabled=True prevents tracing configuration."""

        # Create a test agent and mock model
        agent = RealtimeAgent(name="test_agent", instructions="test")
        agent.handoffs = []

        mock_model = Mock(spec=RealtimeModel)

        # Create session with tracing disabled
        session = RealtimeSession(
            model=mock_model,
            agent=agent,
            context=None,
            model_config=None,
            run_config={"tracing_disabled": True},
        )

        # Test the _get_updated_model_settings_from_agent method directly
        model_settings = await session._get_updated_model_settings_from_agent(
            starting_settings=None, agent=agent
        )

        # When tracing is disabled, model settings should have tracing=None
        assert model_settings["tracing"] is None
