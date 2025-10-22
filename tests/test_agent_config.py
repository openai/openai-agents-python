import pytest
from pydantic import BaseModel

from agents import Agent, AgentOutputSchema, Handoff, RunContextWrapper, handoff
from agents.lifecycle import AgentHooksBase
from agents.model_settings import ModelSettings
from agents.run import AgentRunner


@pytest.mark.asyncio
async def test_system_instructions():
    agent = Agent[None](
        name="test",
        instructions="abc123",
    )
    context = RunContextWrapper(None)

    assert await agent.get_system_prompt(context) == "abc123"

    def sync_instructions(agent: Agent[None], context: RunContextWrapper[None]) -> str:
        return "sync_123"

    agent = agent.clone(instructions=sync_instructions)
    assert await agent.get_system_prompt(context) == "sync_123"

    async def async_instructions(agent: Agent[None], context: RunContextWrapper[None]) -> str:
        return "async_123"

    agent = agent.clone(instructions=async_instructions)
    assert await agent.get_system_prompt(context) == "async_123"


@pytest.mark.asyncio
async def test_handoff_with_agents():
    agent_1 = Agent(
        name="agent_1",
    )

    agent_2 = Agent(
        name="agent_2",
    )

    agent_3 = Agent(
        name="agent_3",
        handoffs=[agent_1, agent_2],
    )

    handoffs = await AgentRunner._get_handoffs(agent_3, RunContextWrapper(None))
    assert len(handoffs) == 2

    assert handoffs[0].agent_name == "agent_1"
    assert handoffs[1].agent_name == "agent_2"

    first_return = await handoffs[0].on_invoke_handoff(RunContextWrapper(None), "")
    assert first_return == agent_1

    second_return = await handoffs[1].on_invoke_handoff(RunContextWrapper(None), "")
    assert second_return == agent_2


@pytest.mark.asyncio
async def test_handoff_with_handoff_obj():
    agent_1 = Agent(
        name="agent_1",
    )

    agent_2 = Agent(
        name="agent_2",
    )

    agent_3 = Agent(
        name="agent_3",
        handoffs=[
            handoff(agent_1),
            handoff(
                agent_2,
                tool_name_override="transfer_to_2",
                tool_description_override="description_2",
            ),
        ],
    )

    handoffs = await AgentRunner._get_handoffs(agent_3, RunContextWrapper(None))
    assert len(handoffs) == 2

    assert handoffs[0].agent_name == "agent_1"
    assert handoffs[1].agent_name == "agent_2"

    assert handoffs[0].tool_name == Handoff.default_tool_name(agent_1)
    assert handoffs[1].tool_name == "transfer_to_2"

    assert handoffs[0].tool_description == Handoff.default_tool_description(agent_1)
    assert handoffs[1].tool_description == "description_2"

    first_return = await handoffs[0].on_invoke_handoff(RunContextWrapper(None), "")
    assert first_return == agent_1

    second_return = await handoffs[1].on_invoke_handoff(RunContextWrapper(None), "")
    assert second_return == agent_2


@pytest.mark.asyncio
async def test_handoff_with_handoff_obj_and_agent():
    agent_1 = Agent(
        name="agent_1",
    )

    agent_2 = Agent(
        name="agent_2",
    )

    agent_3 = Agent(
        name="agent_3",
        handoffs=[handoff(agent_1), agent_2],
    )

    handoffs = await AgentRunner._get_handoffs(agent_3, RunContextWrapper(None))
    assert len(handoffs) == 2

    assert handoffs[0].agent_name == "agent_1"
    assert handoffs[1].agent_name == "agent_2"

    assert handoffs[0].tool_name == Handoff.default_tool_name(agent_1)
    assert handoffs[1].tool_name == Handoff.default_tool_name(agent_2)

    assert handoffs[0].tool_description == Handoff.default_tool_description(agent_1)
    assert handoffs[1].tool_description == Handoff.default_tool_description(agent_2)

    first_return = await handoffs[0].on_invoke_handoff(RunContextWrapper(None), "")
    assert first_return == agent_1

    second_return = await handoffs[1].on_invoke_handoff(RunContextWrapper(None), "")
    assert second_return == agent_2


@pytest.mark.asyncio
async def test_agent_cloning():
    agent = Agent(
        name="test",
        handoff_description="test_description",
        model="o3-mini",
    )

    cloned = agent.clone(
        handoff_description="new_description",
        model="o1",
    )

    assert cloned.name == "test"
    assert cloned.handoff_description == "new_description"
    assert cloned.model == "o1"


class Foo(BaseModel):
    bar: str


@pytest.mark.asyncio
async def test_agent_final_output():
    agent = Agent(
        name="test",
        output_type=Foo,
    )

    schema = AgentRunner._get_output_schema(agent)
    assert isinstance(schema, AgentOutputSchema)
    assert schema is not None
    assert schema.output_type == Foo
    assert schema.is_strict_json_schema() is True
    assert schema.json_schema() is not None
    assert not schema.is_plain_text()


class TestAgentValidation:
    """Essential validation tests for Agent __post_init__"""

    def test_name_validation_critical_cases(self):
        """Test name validation - the original issue that started this PR"""
        # This was the original failing case that caused JSON serialization errors
        with pytest.raises(TypeError, match="Agent name must be a string, got int"):
            Agent(name=1)  # type: ignore

        with pytest.raises(TypeError, match="Agent name must be a string, got NoneType"):
            Agent(name=None)  # type: ignore

    def test_tool_use_behavior_dict_validation(self):
        """Test tool_use_behavior accepts StopAtTools dict - fixes existing test failures"""
        # This test ensures the existing failing tests now pass
        Agent(name="test", tool_use_behavior={"stop_at_tool_names": ["tool1"]})

        # Invalid cases that should fail
        with pytest.raises(TypeError, match="Agent tool_use_behavior must be"):
            Agent(name="test", tool_use_behavior=123)  # type: ignore

    def test_hooks_validation_python39_compatibility(self):
        """Test hooks validation works with Python 3.9 - fixes generic type issues"""

        class MockHooks(AgentHooksBase):
            pass

        # Valid case
        Agent(name="test", hooks=MockHooks())  # type: ignore

        # Invalid case
        with pytest.raises(TypeError, match="Agent hooks must be an AgentHooks instance"):
            Agent(name="test", hooks="invalid")  # type: ignore

    def test_list_field_validation(self):
        """Test critical list fields that commonly get wrong types"""
        # These are the most common mistakes users make
        with pytest.raises(TypeError, match="Agent tools must be a list"):
            Agent(name="test", tools="not_a_list")  # type: ignore

        with pytest.raises(TypeError, match="Agent handoffs must be a list"):
            Agent(name="test", handoffs="not_a_list")  # type: ignore

    def test_tools_content_validation_issue_1443(self):
        """Test that tools list validates each element is a valid Tool object (Issue #1443)

        This test addresses the issue where passing invalid tool types (e.g., raw functions)
        in a list would pass __post_init__ validation but fail later at runtime with:
        AttributeError: 'function' object has no attribute 'name'

        The fix validates each tool in the list during initialization.
        """
        from agents.exceptions import UserError

        def raw_function():
            """A raw function, not decorated with @function_tool"""
            return "test"

        # Case 1: Raw function in tools list should raise UserError at init
        with pytest.raises(
            UserError,
            match=r"tools\[0\] must be a valid Tool object.*got function.*@function_tool",
        ):
            Agent(name="test", tools=[raw_function])  # type: ignore

        # Case 2: String in tools list should raise UserError at init
        with pytest.raises(
            UserError,
            match=r"tools\[0\] must be a valid Tool object.*got str",
        ):
            Agent(name="test", tools=["invalid_string"])  # type: ignore

        # Case 3: Mixed valid and invalid tools - should catch invalid at correct index
        from agents import function_tool

        @function_tool
        def valid_tool() -> str:
            """A valid tool"""
            return "ok"

        with pytest.raises(
            UserError,
            match=r"tools\[1\] must be a valid Tool object.*got str",
        ):
            Agent(name="test", tools=[valid_tool, "invalid"])  # type: ignore

    def test_model_settings_validation(self):
        """Test model_settings validation - prevents runtime errors"""
        # Valid case
        Agent(name="test", model_settings=ModelSettings())

        # Invalid case that could cause runtime issues
        with pytest.raises(
            TypeError, match="Agent model_settings must be a ModelSettings instance"
        ):
            Agent(name="test", model_settings={})  # type: ignore
