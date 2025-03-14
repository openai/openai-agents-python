import pytest
from pydantic import BaseModel

from agents import Agent, Handoff, RunContextWrapper, Runner, handoff, FunctionTool


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

def test_list_tools():
    # Create some mock tools using FunctionTool
    tool1 = FunctionTool(
        name="Tool1",
        description="A mock tool for testing",
        params_json_schema={},
        on_invoke_tool=lambda ctx, input: "output"
    )
    tool2 = FunctionTool(
        name="Tool2",
        description="Another mock tool for testing",
        params_json_schema={},
        on_invoke_tool=lambda ctx, input: "output"
    )
    
    # Initialize the agent with these tools
    agent = Agent(name="TestAgent", tools=[tool1, tool2])
    
    # Get the list of tool names
    tool_names = agent.list_tools()
    
    # Assert that the tool names are as expected
    assert tool_names == ["Tool1", "Tool2"]

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

    handoffs = Runner._get_handoffs(agent_3)
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

    handoffs = Runner._get_handoffs(agent_3)
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

    handoffs = Runner._get_handoffs(agent_3)
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

    schema = Runner._get_output_schema(agent)
    assert schema is not None
    assert schema.output_type == Foo
    assert schema.strict_json_schema is True
    assert schema.json_schema() is not None
    assert not schema.is_plain_text()
