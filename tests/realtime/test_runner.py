from unittest.mock import AsyncMock, Mock, patch

import pytest
from inline_snapshot import snapshot

from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig, RealtimeSessionModelSettings
from agents.realtime.model import RealtimeModel, RealtimeModelConfig
from agents.realtime.runner import RealtimeRunner
from agents.realtime.session import RealtimeSession


class MockRealtimeModel(RealtimeModel):
    async def connect(self, options=None):
        pass

    def add_listener(self, listener):
        pass

    def remove_listener(self, listener):
        pass

    async def send_event(self, event):
        pass

    async def send_message(self, message, other_event_data=None):
        pass

    async def send_audio(self, audio, commit=False):
        pass

    async def send_tool_output(self, tool_call, output, start_response=True):
        pass

    async def interrupt(self):
        pass

    async def close(self):
        pass


@pytest.fixture
def mock_agent():
    agent = Mock(spec=RealtimeAgent)
    agent.get_system_prompt = AsyncMock(return_value="Test instructions")
    agent.get_all_tools = AsyncMock(return_value=[{"type": "function", "name": "test_tool"}])
    return agent


@pytest.fixture
def mock_model():
    return MockRealtimeModel()


@pytest.mark.asyncio
async def test_run_creates_session_with_no_settings(mock_agent, mock_model):
    """Test that run() creates a session correctly if no settings are provided"""
    runner = RealtimeRunner(mock_agent, model=mock_model)

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        session = await runner.run()

        # Verify session was created with correct parameters
        mock_session_class.assert_called_once()
        call_args = mock_session_class.call_args

        assert call_args[1]["model"] == mock_model
        assert call_args[1]["agent"] == mock_agent
        assert call_args[1]["context"] is None

        # Verify model_config contains expected settings from agent
        model_config = call_args[1]["model_config"]
        assert model_config == snapshot(
            {
                "initial_model_settings": {
                    "instructions": "Test instructions",
                    "tools": [{"type": "function", "name": "test_tool"}],
                }
            }
        )

        assert session == mock_session


@pytest.mark.asyncio
async def test_run_creates_session_with_settings_only_in_init(mock_agent, mock_model):
    """Test that it creates a session with the right settings if they are provided only in init"""
    config = RealtimeRunConfig(
        model_settings=RealtimeSessionModelSettings(model_name="gpt-4o-realtime", voice="nova")
    )
    runner = RealtimeRunner(mock_agent, model=mock_model, config=config)

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        _ = await runner.run()

        # Verify session was created with config overrides
        call_args = mock_session_class.call_args
        model_config = call_args[1]["model_config"]

        # Should have agent settings plus config overrides
        assert model_config == snapshot(
            {
                "initial_model_settings": {
                    "instructions": "Test instructions",
                    "tools": [{"type": "function", "name": "test_tool"}],
                    "model_name": "gpt-4o-realtime",
                    "voice": "nova",
                }
            }
        )


@pytest.mark.asyncio
async def test_run_creates_session_with_settings_in_both_init_and_run_overrides(
    mock_agent, mock_model
):
    """Test settings in both init and run() - init should override run()"""
    init_config = RealtimeRunConfig(
        model_settings=RealtimeSessionModelSettings(model_name="gpt-4o-realtime", voice="nova")
    )
    runner = RealtimeRunner(mock_agent, model=mock_model, config=init_config)

    run_model_config: RealtimeModelConfig = {
        "initial_model_settings": RealtimeSessionModelSettings(
            voice="alloy", input_audio_format="pcm16"
        )
    }

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        _ = await runner.run(model_config=run_model_config)

        # Verify run() settings override init settings
        call_args = mock_session_class.call_args
        model_config = call_args[1]["model_config"]

        # Should have agent settings, then init config, then run config overrides
        assert model_config == snapshot(
            {
                "initial_model_settings": {
                    "voice": "nova",
                    "input_audio_format": "pcm16",
                    "instructions": "Test instructions",
                    "tools": [{"type": "function", "name": "test_tool"}],
                    "model_name": "gpt-4o-realtime",
                }
            }
        )


@pytest.mark.asyncio
async def test_run_creates_session_with_settings_only_in_run(mock_agent, mock_model):
    """Test settings provided only in run()"""
    runner = RealtimeRunner(mock_agent, model=mock_model)

    run_model_config: RealtimeModelConfig = {
        "initial_model_settings": RealtimeSessionModelSettings(
            model_name="gpt-4o-realtime-preview", voice="shimmer", modalities=["text", "audio"]
        )
    }

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        _ = await runner.run(model_config=run_model_config)

        # Verify run() settings are applied
        call_args = mock_session_class.call_args
        model_config = call_args[1]["model_config"]

        # Should have agent settings plus run() settings
        assert model_config == snapshot(
            {
                "initial_model_settings": {
                    "model_name": "gpt-4o-realtime-preview",
                    "voice": "shimmer",
                    "modalities": ["text", "audio"],
                    "instructions": "Test instructions",
                    "tools": [{"type": "function", "name": "test_tool"}],
                }
            }
        )


@pytest.mark.asyncio
async def test_run_with_context_parameter(mock_agent, mock_model):
    """Test that context parameter is passed through to session"""
    runner = RealtimeRunner(mock_agent, model=mock_model)
    test_context = {"user_id": "test123"}

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        await runner.run(context=test_context)

        call_args = mock_session_class.call_args
        assert call_args[1]["context"] == test_context


@pytest.mark.asyncio
async def test_get_model_settings_with_none_values(mock_model):
    """Test _get_model_settings handles None values from agent properly"""
    agent = Mock(spec=RealtimeAgent)
    agent.get_system_prompt = AsyncMock(return_value=None)
    agent.get_all_tools = AsyncMock(return_value=None)

    runner = RealtimeRunner(agent, model=mock_model)

    with patch("agents.realtime.runner.RealtimeSession"):
        await runner.run()

        # Should not crash and agent methods should be called
        agent.get_system_prompt.assert_called_once()
        agent.get_all_tools.assert_called_once()
