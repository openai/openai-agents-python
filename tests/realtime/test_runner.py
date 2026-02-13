from unittest.mock import AsyncMock, Mock, patch

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig, RealtimeSessionModelSettings
from agents.realtime.model import RealtimeModel, RealtimeModelConfig
from agents.realtime.runner import RealtimeRunner
from agents.realtime.session import RealtimeSession
from agents.tool import function_tool


class MockRealtimeModel(RealtimeModel):
    def __init__(self):
        self.connect_args = None

    async def connect(self, options=None):
        self.connect_args = options

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
async def test_run_creates_session_with_no_settings(
    mock_agent: Mock, mock_model: MockRealtimeModel
):
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

        # With no settings provided, model_config should be None
        model_config = call_args[1]["model_config"]
        assert model_config is None

        assert session == mock_session


@pytest.mark.asyncio
async def test_run_creates_session_with_settings_only_in_init(
    mock_agent: Mock, mock_model: MockRealtimeModel
):
    """Test that it creates a session with the right settings if they are provided only in init"""
    config = RealtimeRunConfig(
        model_settings=RealtimeSessionModelSettings(model_name="gpt-4o-realtime", voice="nova")
    )
    runner = RealtimeRunner(mock_agent, model=mock_model, config=config)

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        _ = await runner.run()

        # Verify session was created - runner no longer processes settings
        call_args = mock_session_class.call_args
        model_config = call_args[1]["model_config"]

        # Runner should pass None for model_config when none provided to run()
        assert model_config is None


@pytest.mark.asyncio
async def test_run_creates_session_with_settings_in_both_init_and_run_overrides(
    mock_agent: Mock, mock_model: MockRealtimeModel
):
    """Test settings provided in run() parameter are passed through"""
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

        # Verify run() model_config is passed through as-is
        call_args = mock_session_class.call_args
        model_config = call_args[1]["model_config"]

        # Runner should pass the model_config from run() parameter directly
        assert model_config == run_model_config


@pytest.mark.asyncio
async def test_run_creates_session_with_settings_only_in_run(
    mock_agent: Mock, mock_model: MockRealtimeModel
):
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

        # Verify run() model_config is passed through as-is
        call_args = mock_session_class.call_args
        model_config = call_args[1]["model_config"]

        # Runner should pass the model_config from run() parameter directly
        assert model_config == run_model_config


@pytest.mark.asyncio
async def test_run_with_context_parameter(mock_agent: Mock, mock_model: MockRealtimeModel):
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
async def test_run_with_none_values_from_agent_does_not_crash(mock_model: MockRealtimeModel):
    """Test that runner handles agents with None values without crashing"""
    agent = Mock(spec=RealtimeAgent)
    agent.get_system_prompt = AsyncMock(return_value=None)
    agent.get_all_tools = AsyncMock(return_value=None)

    runner = RealtimeRunner(agent, model=mock_model)

    with patch("agents.realtime.runner.RealtimeSession") as mock_session_class:
        mock_session = Mock(spec=RealtimeSession)
        mock_session_class.return_value = mock_session

        session = await runner.run()

        # Should not crash and return session
        assert session == mock_session
        # Runner no longer calls agent methods directly - session does that
        agent.get_system_prompt.assert_not_called()
        agent.get_all_tools.assert_not_called()


@pytest.mark.asyncio
async def test_tool_and_handoffs_are_correct(mock_model: MockRealtimeModel):
    @function_tool
    def tool_one():
        return "result_one"

    agent_1 = RealtimeAgent(
        name="one",
        instructions="instr_one",
    )
    agent_2 = RealtimeAgent(
        name="two",
        instructions="instr_two",
        tools=[tool_one],
        handoffs=[agent_1],
    )

    session = RealtimeSession(
        model=mock_model,
        agent=agent_2,
        context=None,
        model_config=None,
        run_config=None,
    )

    async with session:
        pass

    # Assert that the model.connect() was called with the correct settings
    connect_args = mock_model.connect_args
    assert connect_args is not None
    assert isinstance(connect_args, dict)
    initial_model_settings = connect_args["initial_model_settings"]
    assert initial_model_settings is not None
    assert isinstance(initial_model_settings, dict)
    assert initial_model_settings["instructions"] == "instr_two"
    assert len(initial_model_settings["tools"]) == 1
    tool = initial_model_settings["tools"][0]
    assert tool.name == "tool_one"

    handoffs = initial_model_settings["handoffs"]
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff.tool_name == "transfer_to_one"
    assert handoff.agent_name == "one"
