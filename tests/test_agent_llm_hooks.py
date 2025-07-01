
from typing import Any, List

import pytest

# Core SDK Imports
from agents.agent import Agent
from agents.run import Runner
from agents.lifecycle import AgentHooks
from agents.tool import Tool, function_tool, FunctionTool
from agents.items import ModelResponse
from agents.usage import Usage, InputTokensDetails, OutputTokensDetails
from agents.models.interface import Model

# Types from the openai library used by the SDK
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

# --- 1. Spy Hook Implementation ---
class LoggingAgentHooks(AgentHooks[Any]):
    def __init__(self):
        super().__init__()
        self.called_hooks: List[str] = []

    # Spy on the NEW hooks
    async def on_llm_start(self, *args, **kwargs):
        self.called_hooks.append("on_llm_start")

    async def on_llm_end(self, *args, **kwargs):
        self.called_hooks.append("on_llm_end")

    # Spy on EXISTING hooks to serve as landmarks for sequence verification
    async def on_start(self, *args, **kwargs):
        self.called_hooks.append("on_start")

    async def on_end(self, *args, **kwargs):
        self.called_hooks.append("on_end")

    async def on_tool_start(self, *args, **kwargs):
        self.called_hooks.append("on_tool_start")

    async def on_tool_end(self, *args, **kwargs):
        self.called_hooks.append("on_tool_end")

# --- 2. Mock Model and Tools ---
class MockModel(Model):
    """A mock model that can be configured to either return a chat message or a tool call."""
    def __init__(self):
        self._call_count = 0
        self._should_call_tool = False
        self._tool_to_call: Tool | None = None

    def configure_for_tool_call(self, tool: Tool):
        self._should_call_tool = True
        self._tool_to_call = tool

    def configure_for_chat(self):
        self._should_call_tool = False
        self._tool_to_call = None

    async def get_response(self, *args, **kwargs) -> ModelResponse:
        self._call_count += 1
        response_items: List[Any] = []

        if self._should_call_tool and self._call_count == 1:
            response_items.append(
                ResponseFunctionToolCall(name=self._tool_to_call.name, arguments='{}', call_id="call123", type="function_call")
            )
        else:
            response_items.append(
                ResponseOutputMessage(id="msg1", content=[{"type":"output_text", "text":"Mock response", "annotations":[]}], role="assistant", status="completed", type="message")
            )
        
        mock_usage = Usage(
            requests=1, input_tokens=10, output_tokens=10, total_tokens=20,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0)
        )
        return ModelResponse(output=response_items, usage=mock_usage, response_id="resp123")

    async def stream_response(self, *args, **kwargs):
        final_response = await self.get_response(*args, **kwargs)
        from openai.types.responses import ResponseCompletedEvent
        class MockSDKResponse:
            def __init__(self, id, output, usage): self.id, self.output, self.usage = id, output, usage
        yield ResponseCompletedEvent(response=MockSDKResponse(final_response.response_id, final_response.output, final_response.usage), type="response_completed")

@function_tool
def mock_tool(a: int, b: int) -> int:
    """A mock tool for testing tool call hooks."""
    return a + b

# --- 3. Pytest Fixtures for Test Setup ---
@pytest.fixture
def logging_hooks() -> LoggingAgentHooks:
    """Provides a fresh instance of LoggingAgentHooks for each test."""
    return LoggingAgentHooks()

@pytest.fixture
def chat_agent(logging_hooks: LoggingAgentHooks) -> Agent:
    """Provides an agent configured for a simple chat interaction."""
    mock_model = MockModel()
    mock_model.configure_for_chat()
    return Agent(
        name="ChatAgent",
        instructions="Test agent for chat.",
        model=mock_model,
        hooks=logging_hooks
    )

@pytest.fixture
def tool_agent(logging_hooks: LoggingAgentHooks) -> Agent:
    """Provides an agent configured to use a tool."""
    mock_model = MockModel()
    mock_model.configure_for_tool_call(mock_tool)
    return Agent(
        name="ToolAgent",
        instructions="Test agent for tools.",
        model=mock_model,
        hooks=logging_hooks,
        tools=[mock_tool]
    )

# --- 4. Test Cases Focused on New Hooks ---
@pytest.mark.asyncio
async def test_llm_hooks_fire_in_chat_scenario(
    chat_agent: Agent, logging_hooks: LoggingAgentHooks
):
    """
    Tests that on_llm_start and on_llm_end fire correctly for a chat-only turn.
    """
    await Runner.run(chat_agent, "Hello")
    
    sequence = logging_hooks.called_hooks
    
    expected_sequence = [
        "on_start",
        "on_llm_start",
        "on_llm_end",
        "on_end",
    ]
    assert sequence == expected_sequence

@pytest.mark.asyncio
async def test_llm_hooks_wrap_tool_hooks_in_tool_scenario(
    tool_agent: Agent, logging_hooks: LoggingAgentHooks
):
    """
    Tests that on_llm_start and on_llm_end wrap the tool execution cycle.
    """
    await Runner.run(tool_agent, "Use your tool")

    sequence = logging_hooks.called_hooks

    expected_sequence = [
        "on_start",
        "on_llm_start",
        "on_llm_end",
        "on_tool_start",
        "on_tool_end",
        "on_llm_start",
        "on_llm_end",
        "on_end"
    ]
    assert sequence == expected_sequence

@pytest.mark.asyncio
async def test_no_hooks_run_if_hooks_is_none():
    """
    Ensures that the agent runs without error when agent.hooks is None.
    """
    mock_model = MockModel()
    mock_model.configure_for_chat()
    agent_no_hooks = Agent(
        name="NoHooksAgent",
        instructions="Test agent without hooks.",
        model=mock_model,
        hooks=None
    )
    
    try:
        await Runner.run(agent_no_hooks, "Hello")
    except Exception as e:
        pytest.fail(f"Runner.run failed when agent.hooks was None: {e}")