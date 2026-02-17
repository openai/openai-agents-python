from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from pydantic import BaseModel, Field

from agents import (
    Agent,
    AgentBase,
    AgentToolStreamEvent,
    FunctionTool,
    MessageOutputItem,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    Session,
    SessionSettings,
    ToolApprovalItem,
    TResponseInputItem,
)
from agents.agent_tool_input import StructuredToolInputBuilderOptions
from agents.agent_tool_state import (
    get_agent_tool_state_scope,
    record_agent_tool_run_result,
    set_agent_tool_state_scope,
)
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent
from agents.tool_context import ToolContext
from tests.utils.hitl import make_function_tool_call


class BoolCtx(BaseModel):
    enable_tools: bool


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_bool():
    """Test that agent.as_tool() respects static boolean is_enabled parameter."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create tool with is_enabled=False
    disabled_tool = agent.as_tool(
        tool_name="disabled_agent_tool",
        tool_description="A disabled agent tool",
        is_enabled=False,
    )

    # Create tool with is_enabled=True (default)
    enabled_tool = agent.as_tool(
        tool_name="enabled_agent_tool",
        tool_description="An enabled agent tool",
        is_enabled=True,
    )

    # Create another tool with default is_enabled (should be True)
    default_tool = agent.as_tool(
        tool_name="default_agent_tool",
        tool_description="A default agent tool",
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[disabled_tool, enabled_tool, default_tool],
    )

    # Test with any context
    context = RunContextWrapper(BoolCtx(enable_tools=True))

    # Get all tools - should filter out the disabled one
    tools = await orchestrator.get_all_tools(context)
    tool_names = [tool.name for tool in tools]

    assert "enabled_agent_tool" in tool_names
    assert "default_agent_tool" in tool_names
    assert "disabled_agent_tool" not in tool_names


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_callable():
    """Test that agent.as_tool() respects callable is_enabled parameter."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create tool with callable is_enabled
    async def cond_enabled(ctx: RunContextWrapper[BoolCtx], agent: AgentBase) -> bool:
        return ctx.context.enable_tools

    conditional_tool = agent.as_tool(
        tool_name="conditional_agent_tool",
        tool_description="A conditionally enabled agent tool",
        is_enabled=cond_enabled,
    )

    # Create tool with lambda is_enabled
    lambda_tool = agent.as_tool(
        tool_name="lambda_agent_tool",
        tool_description="A lambda enabled agent tool",
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[conditional_tool, lambda_tool],
    )

    # Test with enable_tools=False
    context_disabled = RunContextWrapper(BoolCtx(enable_tools=False))
    tools_disabled = await orchestrator.get_all_tools(context_disabled)
    assert len(tools_disabled) == 0

    # Test with enable_tools=True
    context_enabled = RunContextWrapper(BoolCtx(enable_tools=True))
    tools_enabled = await orchestrator.get_all_tools(context_enabled)
    tool_names = [tool.name for tool in tools_enabled]

    assert len(tools_enabled) == 2
    assert "conditional_agent_tool" in tool_names
    assert "lambda_agent_tool" in tool_names


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_mixed():
    """Test agent.as_tool() with mixed enabled/disabled tools."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create various tools with different is_enabled configurations
    always_enabled = agent.as_tool(
        tool_name="always_enabled",
        tool_description="Always enabled tool",
        is_enabled=True,
    )

    always_disabled = agent.as_tool(
        tool_name="always_disabled",
        tool_description="Always disabled tool",
        is_enabled=False,
    )

    conditionally_enabled = agent.as_tool(
        tool_name="conditionally_enabled",
        tool_description="Conditionally enabled tool",
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
    )

    default_enabled = agent.as_tool(
        tool_name="default_enabled",
        tool_description="Default enabled tool",
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[always_enabled, always_disabled, conditionally_enabled, default_enabled],
    )

    # Test with enable_tools=False
    context_disabled = RunContextWrapper(BoolCtx(enable_tools=False))
    tools_disabled = await orchestrator.get_all_tools(context_disabled)
    tool_names_disabled = [tool.name for tool in tools_disabled]

    assert len(tools_disabled) == 2
    assert "always_enabled" in tool_names_disabled
    assert "default_enabled" in tool_names_disabled
    assert "always_disabled" not in tool_names_disabled
    assert "conditionally_enabled" not in tool_names_disabled

    # Test with enable_tools=True
    context_enabled = RunContextWrapper(BoolCtx(enable_tools=True))
    tools_enabled = await orchestrator.get_all_tools(context_enabled)
    tool_names_enabled = [tool.name for tool in tools_enabled]

    assert len(tools_enabled) == 3
    assert "always_enabled" in tool_names_enabled
    assert "default_enabled" in tool_names_enabled
    assert "conditionally_enabled" in tool_names_enabled
    assert "always_disabled" not in tool_names_enabled


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_preserves_other_params():
    """Test that is_enabled parameter doesn't interfere with other agent.as_tool() parameters."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that returns a greeting.",
    )

    # Custom output extractor
    async def custom_extractor(result):
        return f"CUSTOM: {result.new_items[-1].text if result.new_items else 'No output'}"

    # Create tool with all parameters including is_enabled
    tool = agent.as_tool(
        tool_name="custom_tool_name",
        tool_description="A custom tool with all parameters",
        custom_output_extractor=custom_extractor,
        is_enabled=True,
    )

    # Verify the tool was created with correct properties
    assert tool.name == "custom_tool_name"
    assert isinstance(tool, FunctionTool)
    assert tool.description == "A custom tool with all parameters"
    assert tool.is_enabled is True

    # Verify tool is included when enabled
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[tool],
    )

    context = RunContextWrapper(BoolCtx(enable_tools=True))
    tools = await orchestrator.get_all_tools(context)
    assert len(tools) == 1
    assert tools[0].name == "custom_tool_name"


@pytest.mark.asyncio
async def test_agent_as_tool_returns_final_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent tool should return final_output when no custom extractor is provided."""

    agent = Agent(name="storyteller")

    result = type(
        "DummyResult",
        (),
        {"final_output": "Hello world"},
    )()

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        return result

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="story_tool",
        tool_description="Tell a short story",
        is_enabled=True,
    )

    assert isinstance(tool, FunctionTool)
    tool_context = ToolContext(
        context=None,
        tool_name="story_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "hello"}')

    assert output == "Hello world"


@pytest.mark.asyncio
async def test_agent_as_tool_custom_output_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom output extractors should receive the RunResult from Runner.run."""

    agent = Agent(name="summarizer")

    message = ResponseOutputMessage(
        id="msg_2",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputText(
                annotations=[],
                text="Original text",
                type="output_text",
                logprobs=[],
            )
        ],
    )

    class DummySession(Session):
        session_id = "sess_123"
        session_settings = SessionSettings()

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            return []

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            return None

        async def pop_item(self) -> TResponseInputItem | None:
            return None

        async def clear_session(self) -> None:
            return None

    dummy_session = DummySession()

    class DummyResult:
        def __init__(self, items: list[MessageOutputItem]) -> None:
            self.new_items = items

    run_result = DummyResult([MessageOutputItem(agent=agent, raw_item=message)])

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "summarize this"
        assert isinstance(context, ToolContext)
        assert context.tool_call_id == "call_2"
        assert context.tool_name == "summary_tool"
        assert max_turns == 7
        assert hooks is hooks_obj
        assert run_config is run_config_obj
        assert previous_response_id == "resp_1"
        assert conversation_id == "conv_1"
        assert session is dummy_session
        return run_result

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    async def extractor(result) -> str:
        assert result is run_result
        return "custom output"

    hooks_obj = RunHooks[Any]()
    run_config_obj = RunConfig(model="gpt-4.1-mini")

    tool = agent.as_tool(
        tool_name="summary_tool",
        tool_description="Summarize input",
        custom_output_extractor=extractor,
        is_enabled=True,
        run_config=run_config_obj,
        max_turns=7,
        hooks=hooks_obj,
        previous_response_id="resp_1",
        conversation_id="conv_1",
        session=dummy_session,
    )

    assert isinstance(tool, FunctionTool)
    tool_context = ToolContext(
        context=None,
        tool_name="summary_tool",
        tool_call_id="call_2",
        tool_arguments='{"input": "summarize this"}',
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "summarize this"}')

    assert output == "custom output"


@pytest.mark.asyncio
async def test_agent_as_tool_inherits_parent_run_config_when_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="inherits_config_agent")
    parent_run_config = RunConfig(model="gpt-4.1-mini")

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        assert isinstance(context, ToolContext)
        assert run_config is parent_run_config
        assert context.run_config is parent_run_config
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="inherits_config_tool",
        tool_description="inherit config",
    )
    tool_context = ToolContext(
        context=None,
        tool_name="inherits_config_tool",
        tool_call_id="call_inherit",
        tool_arguments='{"input":"hello"}',
        run_config=parent_run_config,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input":"hello"}')

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_explicit_run_config_overrides_parent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="override_config_agent")
    parent_run_config = RunConfig(model="gpt-4.1-mini")
    explicit_run_config = RunConfig(model="gpt-4.1")

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        assert isinstance(context, ToolContext)
        assert run_config is explicit_run_config
        assert context.run_config is explicit_run_config
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="override_config_tool",
        tool_description="override config",
        run_config=explicit_run_config,
    )
    tool_context = ToolContext(
        context=None,
        tool_name="override_config_tool",
        tool_call_id="call_override",
        tool_arguments='{"input":"hello"}',
        run_config=parent_run_config,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input":"hello"}')

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_inherits_trace_include_sensitive_data_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="trace_config_agent")
    parent_run_config = RunConfig(trace_include_sensitive_data=False)

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        assert isinstance(context, ToolContext)
        assert run_config is parent_run_config
        assert run_config.trace_include_sensitive_data is False
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="trace_config_tool",
        tool_description="inherits trace config",
    )
    tool_context = ToolContext(
        context=None,
        tool_name="trace_config_tool",
        tool_call_id="call_trace",
        tool_arguments='{"input":"hello"}',
        run_config=parent_run_config,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input":"hello"}')

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_structured_input_sets_tool_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured agent tools should capture input data and pass JSON to the nested run."""

    class TranslationInput(BaseModel):
        text: str
        source: str
        target: str

    agent = Agent(name="translator")
    tool = agent.as_tool(
        tool_name="translate",
        tool_description="Translate text",
        parameters=TranslationInput,
    )

    captured: dict[str, Any] = {}

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        captured["input"] = input
        captured["context"] = context
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    run_context = RunContextWrapper({"locale": "en-US"})
    args = {"text": "hola", "source": "es", "target": "en"}
    tool_context = ToolContext(
        context=run_context.context,
        usage=run_context.usage,
        tool_name="translate",
        tool_call_id="call_structured",
        tool_arguments=json.dumps(args),
    )

    await tool.on_invoke_tool(tool_context, json.dumps(args))

    called_input = captured["input"]
    assert isinstance(called_input, str)
    assert json.loads(called_input) == args

    nested_context = captured["context"]
    assert isinstance(nested_context, ToolContext)
    assert nested_context.context is run_context.context
    assert nested_context.usage is run_context.usage
    assert nested_context.tool_input == args
    assert run_context.tool_input is None


@pytest.mark.asyncio
async def test_agent_as_tool_clears_stale_tool_input_for_plain_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-structured agent tools should not inherit stale tool input."""

    agent = Agent(name="plain_agent")
    tool = agent.as_tool(
        tool_name="plain_tool",
        tool_description="Plain tool",
    )

    run_context = RunContextWrapper({"locale": "en-US"})
    run_context.tool_input = {"text": "bonjour"}

    tool_context = ToolContext(
        context=run_context.context,
        usage=run_context.usage,
        tool_name="plain_tool",
        tool_call_id="call_plain",
        tool_arguments='{"input": "hello"}',
    )
    tool_context.tool_input = run_context.tool_input

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert isinstance(context, ToolContext)
        assert context.tool_input is None
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    await tool.on_invoke_tool(tool_context, '{"input": "hello"}')

    assert run_context.tool_input == {"text": "bonjour"}


@pytest.mark.asyncio
async def test_agent_as_tool_includes_schema_summary_with_descriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema descriptions should be summarized for structured inputs."""

    class TranslationInput(BaseModel):
        text: str = Field(description="Text to translate")
        target: str = Field(description="Target language")

    agent = Agent(name="summary_agent")
    tool = agent.as_tool(
        tool_name="summarize_schema",
        tool_description="Summary tool",
        parameters=TranslationInput,
    )

    captured: dict[str, Any] = {}

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        captured["input"] = input
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    args = {"text": "hola", "target": "en"}
    tool_context = ToolContext(
        context=None,
        tool_name="summarize_schema",
        tool_call_id="call_summary",
        tool_arguments=json.dumps(args),
    )

    await tool.on_invoke_tool(tool_context, json.dumps(args))

    called_input = captured["input"]
    assert isinstance(called_input, str)
    assert "Input Schema Summary:" in called_input
    assert "text (string, required)" in called_input
    assert "Text to translate" in called_input
    assert "target (string, required)" in called_input
    assert "Target language" in called_input
    assert '"text": "hola"' in called_input
    assert '"target": "en"' in called_input


@pytest.mark.asyncio
async def test_agent_as_tool_supports_custom_input_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom input builders should supply nested input items."""

    class TranslationInput(BaseModel):
        text: str

    agent = Agent(name="builder_agent")
    builder_calls: list[StructuredToolInputBuilderOptions] = []
    custom_items = [{"role": "user", "content": "custom input"}]

    async def builder(options: StructuredToolInputBuilderOptions):
        builder_calls.append(options)
        return custom_items

    tool = agent.as_tool(
        tool_name="builder_tool",
        tool_description="Builder tool",
        parameters=TranslationInput,
        input_builder=builder,
    )

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert input == custom_items
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    args = {"text": "hola"}
    tool_context = ToolContext(
        context=None,
        tool_name="builder_tool",
        tool_call_id="call_builder",
        tool_arguments=json.dumps(args),
    )

    await tool.on_invoke_tool(tool_context, json.dumps(args))

    assert builder_calls
    assert builder_calls[0]["params"] == args
    assert builder_calls[0]["summary"] is None
    assert builder_calls[0]["json_schema"] is None


@pytest.mark.asyncio
async def test_agent_as_tool_rejects_invalid_builder_output() -> None:
    """Invalid builder output should surface as a tool error."""

    agent = Agent(name="invalid_builder_agent")

    def builder(_options):
        return 123

    tool = agent.as_tool(
        tool_name="invalid_builder_tool",
        tool_description="Invalid builder tool",
        input_builder=builder,
    )

    tool_context = ToolContext(
        context=None,
        tool_name="invalid_builder_tool",
        tool_call_id="call_invalid_builder",
        tool_arguments='{"input": "hi"}',
    )
    result = await tool.on_invoke_tool(tool_context, '{"input": "hi"}')

    assert "Agent tool called with invalid input" in result


@pytest.mark.asyncio
async def test_agent_as_tool_includes_json_schema_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_input_schema should embed the full JSON schema."""

    class TranslationInput(BaseModel):
        text: str = Field(description="Text to translate")
        target: str = Field(description="Target language")

    agent = Agent(name="schema_agent")
    tool = agent.as_tool(
        tool_name="schema_tool",
        tool_description="Schema tool",
        parameters=TranslationInput,
        include_input_schema=True,
    )

    captured: dict[str, Any] = {}

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        captured["input"] = input
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    args = {"text": "hola", "target": "en"}
    tool_context = ToolContext(
        context=None,
        tool_name="schema_tool",
        tool_call_id="call_schema",
        tool_arguments=json.dumps(args),
    )

    await tool.on_invoke_tool(tool_context, json.dumps(args))

    called_input = captured["input"]
    assert isinstance(called_input, str)
    assert "Input JSON Schema:" in called_input
    assert '"properties"' in called_input
    assert '"text"' in called_input
    assert '"target"' in called_input


@pytest.mark.asyncio
async def test_agent_as_tool_ignores_input_schema_without_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_input_schema should be ignored when no parameters are provided."""

    agent = Agent(name="default_schema_agent")
    tool = agent.as_tool(
        tool_name="default_schema_tool",
        tool_description="Default schema tool",
        include_input_schema=True,
    )

    captured: dict[str, Any] = {}

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        captured["input"] = input
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool_context = ToolContext(
        context=None,
        tool_name="default_schema_tool",
        tool_call_id="call_default_schema",
        tool_arguments='{"input": "hello"}',
    )

    await tool.on_invoke_tool(tool_context, '{"input": "hello"}')

    assert captured["input"] == "hello"
    assert "properties" in tool.params_json_schema


@pytest.mark.asyncio
async def test_agent_as_tool_rejected_nested_approval_resumes_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected nested approvals should resume the pending run with rejection applied."""

    agent = Agent(name="outer")
    tool_call = make_function_tool_call(
        "outer_tool",
        call_id="outer-1",
        arguments='{"input": "hello"}',
    )
    tool_context = ToolContext(
        context=None,
        tool_name="outer_tool",
        tool_call_id="outer-1",
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    inner_call = make_function_tool_call("inner_tool", call_id="inner-1")
    approval_item = ToolApprovalItem(agent=agent, raw_item=inner_call)

    class DummyState:
        def __init__(self, nested_context: ToolContext) -> None:
            self._context = nested_context

    class DummyPendingResult:
        def __init__(self) -> None:
            self.interruptions = [approval_item]
            self.final_output = None

        def to_state(self) -> DummyState:
            return resume_state

    class DummyResumedResult:
        def __init__(self) -> None:
            self.interruptions: list[ToolApprovalItem] = []
            self.final_output = "rejected"

    nested_context = ToolContext(
        context=None,
        tool_name=tool_call.name,
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )
    resume_state = DummyState(nested_context)
    pending_result = DummyPendingResult()
    record_agent_tool_run_result(tool_call, cast(Any, pending_result))
    tool_context.reject_tool(approval_item)

    resumed_result = DummyResumedResult()
    run_inputs: list[Any] = []

    async def run_resume(cls, /, starting_agent, input, **kwargs) -> DummyResumedResult:
        run_inputs.append(input)
        assert input is resume_state
        assert input._context is not None
        assert input._context.is_tool_approved("inner_tool", "inner-1") is False
        return resumed_result

    monkeypatch.setattr(Runner, "run", classmethod(run_resume))

    async def extractor(result: Any) -> str:
        assert result is resumed_result
        return "from_resume"

    tool = agent.as_tool(
        tool_name="outer_tool",
        tool_description="Outer agent tool",
        custom_output_extractor=extractor,
        is_enabled=True,
    )

    output = await tool.on_invoke_tool(tool_context, tool_call.arguments)

    assert output == "from_resume"
    assert run_inputs == [resume_state]


@pytest.mark.asyncio
async def test_agent_as_tool_preserves_scope_for_nested_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested ToolContext instances should inherit the parent tool-state scope."""

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.interruptions: list[ToolApprovalItem] = []

    scope_id = "resume-scope"
    agent = Agent(name="scope-agent")
    tool = agent.as_tool(tool_name="scope_tool", tool_description="Scope tool")

    async def fake_run(cls, /, starting_agent, input, **kwargs) -> DummyResult:
        del cls, starting_agent, input
        nested_context = kwargs.get("context")
        assert isinstance(nested_context, ToolContext)
        assert get_agent_tool_state_scope(nested_context) == scope_id
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool_context = ToolContext(
        context=None,
        tool_name="scope_tool",
        tool_call_id="scope-call",
        tool_arguments='{"input":"hello"}',
    )
    set_agent_tool_state_scope(tool_context, scope_id)

    output = await tool.on_invoke_tool(tool_context, '{"input":"hello"}')
    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_preserves_scope_for_nested_run_context_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested RunContextWrapper instances should inherit the parent tool-state scope."""

    class Params(BaseModel):
        text: str

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.interruptions: list[ToolApprovalItem] = []

    scope_id = "resume-scope-wrapper"
    agent = Agent(name="scope-agent-wrapper")
    tool = agent.as_tool(
        tool_name="scope_tool_wrapper",
        tool_description="Scope tool wrapper",
        parameters=Params,
    )

    async def fake_run(cls, /, starting_agent, input, **kwargs) -> DummyResult:
        del cls, starting_agent, input
        nested_context = kwargs.get("context")
        assert isinstance(nested_context, RunContextWrapper)
        assert get_agent_tool_state_scope(nested_context) == scope_id
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    parent_context = RunContextWrapper(context={"key": "value"})
    set_agent_tool_state_scope(parent_context, scope_id)

    output = await tool.on_invoke_tool(cast(Any, parent_context), '{"text":"hello"}')
    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_streams_events_with_on_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="streamer")
    stream_events = [
        RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"})),
        RawResponsesStreamEvent(data=cast(Any, {"type": "output_text_delta", "delta": "hi"})),
    ]

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "streamed output"
            self.current_agent = agent

        async def stream_events(self):
            for ev in stream_events:
                yield ev

    run_calls: list[dict[str, Any]] = []

    def fake_run_streamed(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        run_calls.append(
            {
                "starting_agent": starting_agent,
                "input": input,
                "context": context,
                "max_turns": max_turns,
                "hooks": hooks,
                "run_config": run_config,
                "previous_response_id": previous_response_id,
                "conversation_id": conversation_id,
                "session": session,
            }
        )
        return DummyStreamingResult()

    async def unexpected_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Runner.run should not be called when on_stream is provided.")

    monkeypatch.setattr(Runner, "run_streamed", classmethod(fake_run_streamed))
    monkeypatch.setattr(Runner, "run", classmethod(unexpected_run))

    received_events: list[AgentToolStreamEvent] = []

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        received_events.append(payload)

    tool_call = ResponseFunctionToolCall(
        id="call_123",
        arguments='{"input": "run streaming"}',
        call_id="call-123",
        name="stream_tool",
        type="function_call",
    )

    tool = agent.as_tool(
        tool_name="stream_tool",
        tool_description="Streams events",
        on_stream=on_stream,
    )

    tool_context = ToolContext(
        context=None,
        tool_name="stream_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "run streaming"}')

    assert output == "streamed output"
    assert len(received_events) == len(stream_events)
    assert received_events[0]["agent"] is agent
    assert received_events[0]["tool_call"] is tool_call
    assert received_events[0]["event"] == stream_events[0]
    assert run_calls[0]["input"] == "run streaming"


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_updates_agent_on_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_agent = Agent(name="primary")
    handed_off_agent = Agent(name="delegate")

    events = [
        AgentUpdatedStreamEvent(new_agent=first_agent),
        RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"})),
        AgentUpdatedStreamEvent(new_agent=handed_off_agent),
        RawResponsesStreamEvent(data=cast(Any, {"type": "output_text_delta", "delta": "hello"})),
    ]

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "delegated output"
            self.current_agent = first_agent

        async def stream_events(self):
            for ev in events:
                yield ev

    def fake_run_streamed(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        return DummyStreamingResult()

    monkeypatch.setattr(Runner, "run_streamed", classmethod(fake_run_streamed))
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    seen_agents: list[Agent[Any]] = []

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        seen_agents.append(payload["agent"])

    tool = first_agent.as_tool(
        tool_name="delegate_tool",
        tool_description="Streams handoff events",
        on_stream=on_stream,
    )

    tool_call = ResponseFunctionToolCall(
        id="call_delegate",
        arguments='{"input": "handoff"}',
        call_id="call-delegate",
        name="delegate_tool",
        type="function_call",
    )
    tool_context = ToolContext(
        context=None,
        tool_name="delegate_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "handoff"}')

    assert output == "delegated output"
    assert seen_agents == [first_agent, first_agent, handed_off_agent, handed_off_agent]


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_works_with_custom_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="streamer")
    stream_events = [RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))]
    stream_events = [RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))]

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "raw output"
            self.current_agent = agent

        async def stream_events(self):
            for ev in stream_events:
                yield ev

    streamed_instance = DummyStreamingResult()

    def fake_run_streamed(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        return streamed_instance

    async def unexpected_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Runner.run should not be called when on_stream is provided.")

    monkeypatch.setattr(Runner, "run_streamed", classmethod(fake_run_streamed))
    monkeypatch.setattr(Runner, "run", classmethod(unexpected_run))

    received: list[Any] = []

    async def extractor(result) -> str:
        received.append(result)
        return "custom value"

    callbacks: list[Any] = []

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        callbacks.append(payload["event"])

    tool_call = ResponseFunctionToolCall(
        id="call_abc",
        arguments='{"input": "stream please"}',
        call_id="call-abc",
        name="stream_tool",
        type="function_call",
    )

    tool = agent.as_tool(
        tool_name="stream_tool",
        tool_description="Streams events",
        custom_output_extractor=extractor,
        on_stream=on_stream,
    )

    tool_context = ToolContext(
        context=None,
        tool_name="stream_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "stream please"}')

    assert output == "custom value"
    assert received == [streamed_instance]
    assert callbacks == stream_events


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_accepts_sync_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="sync_handler_agent")

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))

    monkeypatch.setattr(
        Runner, "run_streamed", classmethod(lambda *args, **kwargs: DummyStreamingResult())
    )
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    calls: list[str] = []

    def sync_handler(event: AgentToolStreamEvent) -> None:
        calls.append(event["event"].type)

    tool_call = ResponseFunctionToolCall(
        id="call_sync",
        arguments='{"input": "go"}',
        call_id="call-sync",
        name="sync_tool",
        type="function_call",
    )

    tool = agent.as_tool(
        tool_name="sync_tool",
        tool_description="Uses sync handler",
        on_stream=sync_handler,
    )
    tool_context = ToolContext(
        context=None,
        tool_name="sync_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "go"}')

    assert output == "ok"
    assert calls == ["raw_response_event"]


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_dispatches_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_stream handlers should not block streaming iteration."""
    agent = Agent(name="nonblocking_agent")

    first_handler_started = asyncio.Event()
    allow_handler_to_continue = asyncio.Event()
    second_event_yielded = asyncio.Event()
    second_event_handled = asyncio.Event()

    first_event = RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))
    second_event = RawResponsesStreamEvent(
        data=cast(Any, {"type": "output_text_delta", "delta": "hi"})
    )

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield first_event
            second_event_yielded.set()
            yield second_event

    dummy_result = DummyStreamingResult()

    monkeypatch.setattr(Runner, "run_streamed", classmethod(lambda *args, **kwargs: dummy_result))
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        if payload["event"] is first_event:
            first_handler_started.set()
            await allow_handler_to_continue.wait()
        else:
            second_event_handled.set()

    tool_call = ResponseFunctionToolCall(
        id="call_nonblocking",
        arguments='{"input": "go"}',
        call_id="call-nonblocking",
        name="nonblocking_tool",
        type="function_call",
    )

    tool = agent.as_tool(
        tool_name="nonblocking_tool",
        tool_description="Uses non-blocking streaming handler",
        on_stream=on_stream,
    )
    tool_context = ToolContext(
        context=None,
        tool_name="nonblocking_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    async def _invoke_tool() -> Any:
        return await tool.on_invoke_tool(tool_context, '{"input": "go"}')

    invoke_task: asyncio.Task[Any] = asyncio.create_task(_invoke_tool())

    await asyncio.wait_for(first_handler_started.wait(), timeout=1.0)
    await asyncio.wait_for(second_event_yielded.wait(), timeout=1.0)
    assert invoke_task.done() is False

    allow_handler_to_continue.set()
    await asyncio.wait_for(second_event_handled.wait(), timeout=1.0)
    output = await asyncio.wait_for(invoke_task, timeout=1.0)

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_handler_exception_does_not_fail_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="handler_error_agent")

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))

    monkeypatch.setattr(
        Runner, "run_streamed", classmethod(lambda *args, **kwargs: DummyStreamingResult())
    )
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    def bad_handler(event: AgentToolStreamEvent) -> None:
        raise RuntimeError("boom")

    tool_call = ResponseFunctionToolCall(
        id="call_bad",
        arguments='{"input": "go"}',
        call_id="call-bad",
        name="error_tool",
        type="function_call",
    )

    tool = agent.as_tool(
        tool_name="error_tool",
        tool_description="Handler throws",
        on_stream=bad_handler,
    )
    tool_context = ToolContext(
        context=None,
        tool_name="error_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "go"}')

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_without_stream_uses_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="nostream_agent")

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "plain"

    run_calls: list[dict[str, Any]] = []

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        run_calls.append({"input": input})
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))
    monkeypatch.setattr(
        Runner,
        "run_streamed",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no stream"))),
    )

    tool = agent.as_tool(
        tool_name="nostream_tool",
        tool_description="No streaming path",
    )
    tool_context = ToolContext(
        context=None,
        tool_name="nostream_tool",
        tool_call_id="call-no",
        tool_arguments='{"input": "plain"}',
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "plain"}')

    assert output == "plain"
    assert run_calls == [{"input": "plain"}]


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_sets_tool_call_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="direct_invocation_agent")

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))

    monkeypatch.setattr(
        Runner, "run_streamed", classmethod(lambda *args, **kwargs: DummyStreamingResult())
    )
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    captured: list[AgentToolStreamEvent] = []

    async def on_stream(event: AgentToolStreamEvent) -> None:
        captured.append(event)

    tool_call = ResponseFunctionToolCall(
        id="call_direct",
        arguments='{"input": "hi"}',
        call_id="direct-call-id",
        name="direct_stream_tool",
        type="function_call",
    )

    tool = agent.as_tool(
        tool_name="direct_stream_tool",
        tool_description="Direct invocation",
        on_stream=on_stream,
    )
    tool_context = ToolContext(
        context=None,
        tool_name="direct_stream_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "hi"}')

    assert output == "ok"
    assert captured[0]["tool_call"] is tool_call


@pytest.mark.asyncio
async def test_agent_as_tool_failure_error_function_none_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If failure_error_function=None, exceptions should propagate to the caller."""
    agent = Agent(name="failing_agent")

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        raise RuntimeError("test failure")

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="failing_agent_tool",
        tool_description="Agent tool that raises",
        is_enabled=True,
        failure_error_function=None,
    )

    assert isinstance(tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="failing_agent_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )

    with pytest.raises(RuntimeError, match="test failure"):
        await tool.on_invoke_tool(tool_context, '{"input": "hello"}')


@pytest.mark.asyncio
async def test_agent_as_tool_failure_error_function_custom_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom failure_error_function should be used to convert exceptions into tool output."""
    agent = Agent(name="failing_agent")

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        raise ValueError("test failure")

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    def custom_failure_handler(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"handled:{type(error).__name__}:{error}"

    tool = agent.as_tool(
        tool_name="failing_agent_tool",
        tool_description="Agent tool that raises",
        is_enabled=True,
        failure_error_function=custom_failure_handler,
    )

    assert isinstance(tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="failing_agent_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )

    result = await tool.on_invoke_tool(tool_context, '{"input": "hello"}')
    assert result == "handled:ValueError:test failure"
