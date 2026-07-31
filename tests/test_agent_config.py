from typing import Any

import pytest
from openai.types.shared import Reasoning
from pydantic import BaseModel

from agents import Agent, AgentOutputSchema, Handoff, RunContextWrapper, handoff
from agents.lifecycle import AgentHooksBase
from agents.model_settings import ModelSettings
from agents.retry import ModelRetryBackoffSettings
from agents.run_internal.run_loop import get_handoffs, get_output_schema


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

    class AsyncCallableInstructions:
        async def __call__(self, context: RunContextWrapper[None], agent: Agent[None]) -> str:
            return "async_callable_123"

    agent = agent.clone(instructions=AsyncCallableInstructions())
    assert await agent.get_system_prompt(context) == "async_callable_123"


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

    handoffs = await get_handoffs(agent_3, RunContextWrapper(None))
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

    handoffs = await get_handoffs(agent_3, RunContextWrapper(None))
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

    handoffs = await get_handoffs(agent_3, RunContextWrapper(None))
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

    schema = get_output_schema(agent)
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

    def test_hooks_validation_type_compatibility(self):
        """Test hooks validation works with generic type validation."""

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

    def test_model_settings_validation(self):
        """Test model_settings validation - prevents runtime errors"""
        # Typed settings and SDK-owned dictionaries are both valid.
        Agent(name="test", model_settings=ModelSettings())
        agent = Agent(name="test", model_settings={"temperature": 0.25})

        assert isinstance(agent.model_settings, ModelSettings)
        assert agent.model_settings.temperature == 0.25

        # Invalid values are rejected before model execution.
        with pytest.raises(
            TypeError, match="Agent model_settings must be a ModelSettings instance or a dict"
        ):
            Agent(name="test", model_settings="invalid")  # type: ignore[arg-type]


def test_agent_model_settings_dictionary_preserves_openai_reasoning_extensions() -> None:
    agent = Agent(
        name="test",
        model_settings={
            "reasoning": {"context": "all_turns", "future_reasoning_option": "enabled"},
            "context_management": [{"type": "compaction", "compact_threshold": 244800}],
            "retry": {"max_retries": 0, "backoff": {"jitter": False}},
        },
    )

    assert isinstance(agent.model_settings.reasoning, Reasoning)
    assert agent.model_settings.reasoning.context == "all_turns"
    assert agent.model_settings.reasoning.model_extra == {"future_reasoning_option": "enabled"}
    assert agent.model_settings.context_management == [
        {"type": "compaction", "compact_threshold": 244800}
    ]
    assert agent.model_settings.retry is not None
    assert agent.model_settings.retry.max_retries == 0
    assert isinstance(agent.model_settings.retry.backoff, ModelRetryBackoffSettings)
    assert agent.model_settings.retry.backoff.jitter is False


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"temperatur": 0.2}, "Unknown model settings: temperatur"),
        ({"retry": {"max_retry": 2}}, "Unknown model settings in retry: max_retry"),
        (
            {"retry": {"backoff": {"initial_delai": 1}}},
            "Unknown model settings in retry.backoff: initial_delai",
        ),
        (
            {"context_management": [{"type": "compaction", "compact_threshold_typo": 1}]},
            r"Unknown model settings in context_management\[0\]: compact_threshold_typo",
        ),
    ],
)
def test_agent_rejects_unknown_first_party_dictionary_model_settings(
    settings: dict[str, Any], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        Agent(name="test", model_settings=settings)


@pytest.mark.parametrize("setting_name", ["reasoning", "context_management", "temperature"])
def test_agent_does_not_promote_model_settings_to_constructor(setting_name: str) -> None:
    arguments: dict[str, Any] = {setting_name: None}
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{setting_name}'"):
        Agent(name="test", **arguments)
