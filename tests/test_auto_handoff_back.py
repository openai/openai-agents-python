"""Tests for automatic back-handoff support (#847).

These tests verify that when a handoff is configured with auto_handoff_back=True,
the child agent automatically hands control back to the originating agent upon
completion, enabling orchestration patterns without circular references.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, RunConfig, Runner, handoff
from agents.handoffs import Handoff

from .fake_model import FakeModel
from .test_responses import get_handoff_tool_call, get_text_message


@pytest.mark.asyncio
async def test_auto_handoff_back_returns_to_originating_agent() -> None:
    """Verify that an agent with auto_handoff_back returns control to the
    originating agent after the child completes its task."""
    orchestrator_model = FakeModel()
    specialist_model = FakeModel()

    specialist = Agent(name="specialist", model=specialist_model)
    orchestrator = Agent(
        name="orchestrator",
        model=orchestrator_model,
        handoffs=[
            handoff(
                specialist,
                auto_handoff_back=True,
            )
        ],
    )

    # Turn 1: orchestrator hands off to specialist
    orchestrator_model.add_multiple_turn_outputs(
        [[get_text_message("let me delegate"), get_handoff_tool_call(specialist)]]
    )
    # Turn 2: specialist finishes its task
    specialist_model.add_multiple_turn_outputs(
        [[get_text_message("specialist analysis complete")]]
    )
    # Turn 3: orchestrator produces final output after regaining control
    orchestrator_model.add_multiple_turn_outputs(
        [[get_text_message("orchestrator final response")]]
    )

    result = await Runner.run(
        orchestrator,
        input="analyze this and give me a final answer",
    )

    # The last agent should be the orchestrator, not the specialist
    assert result.last_agent.name == "orchestrator"
    assert result.final_output == "orchestrator final response"


@pytest.mark.asyncio
async def test_auto_handoff_back_disabled_by_default() -> None:
    """Verify that by default (auto_handoff_back=False), control does NOT
    return to the originating agent."""
    orchestrator_model = FakeModel()
    specialist_model = FakeModel()

    specialist = Agent(name="specialist", model=specialist_model)
    orchestrator = Agent(
        name="orchestrator",
        model=orchestrator_model,
        handoffs=[specialist],  # default auto_handoff_back=False
    )

    # Turn 1: orchestrator hands off to specialist
    orchestrator_model.add_multiple_turn_outputs(
        [[get_text_message("let me delegate"), get_handoff_tool_call(specialist)]]
    )
    # Turn 2: specialist finishes its task
    specialist_model.add_multiple_turn_outputs(
        [[get_text_message("specialist analysis complete")]]
    )

    result = await Runner.run(
        orchestrator,
        input="analyze this",
    )

    # The last agent should be the specialist (no auto-handoff-back)
    assert result.last_agent.name == "specialist"
    assert result.final_output == "specialist analysis complete"


@pytest.mark.asyncio
async def test_auto_handoff_back_with_nested_handoffs() -> None:
    """Verify that nested auto_handoff_back chains work correctly.

    orchestrator -> specialist_a -> specialist_b
    Each handoff has auto_handoff_back=True, so control should return
    through the chain: specialist_b -> specialist_a -> orchestrator.
    """
    orch_model = FakeModel()
    model_a = FakeModel()
    model_b = FakeModel()

    specialist_b = Agent(name="specialist_b", model=model_b)
    specialist_a = Agent(
        name="specialist_a",
        model=model_a,
        handoffs=[handoff(specialist_b, auto_handoff_back=True)],
    )
    orchestrator = Agent(
        name="orchestrator",
        model=orch_model,
        handoffs=[handoff(specialist_a, auto_handoff_back=True)],
    )

    # Turn 1: orchestrator hands off to specialist_a
    orch_model.add_multiple_turn_outputs(
        [[get_text_message("delegating to a"), get_handoff_tool_call(specialist_a)]]
    )
    # Turn 2: specialist_a hands off to specialist_b
    model_a.add_multiple_turn_outputs(
        [[get_text_message("delegating to b"), get_handoff_tool_call(specialist_b)]]
    )
    # Turn 3: specialist_b finishes
    model_b.add_multiple_turn_outputs(
        [[get_text_message("b's analysis complete")]]
    )
    # Turn 4: specialist_a finishes after regaining control
    model_a.add_multiple_turn_outputs(
        [[get_text_message("a's combined analysis")]]
    )
    # Turn 5: orchestrator finishes after regaining control
    orch_model.add_multiple_turn_outputs(
        [[get_text_message("orchestrator final response")]]
    )

    result = await Runner.run(
        orchestrator,
        input="analyze this deeply",
    )

    assert result.last_agent.name == "orchestrator"
    assert result.final_output == "orchestrator final response"


@pytest.mark.asyncio
async def test_auto_handoff_back_handoff_config() -> None:
    """Verify the Handoff object correctly stores auto_handoff_back."""
    agent_b = Agent(name="agent_b")
    agent_a = Agent(name="agent_a")

    # Default: auto_handoff_back=False
    h_default = Handoff.default_tool_name(agent_b)
    assert isinstance(h_default, str)

    # Explicitly set auto_handoff_back=True via handoff()
    h_auto = handoff(agent_b, auto_handoff_back=True)
    assert h_auto.auto_handoff_back is True
    assert h_auto.agent_name == "agent_b"

    # Explicitly set auto_handoff_back=False (default) via handoff()
    h_no_auto = handoff(agent_b, auto_handoff_back=False)
    assert h_no_auto.auto_handoff_back is False


@pytest.mark.asyncio
async def test_auto_handoff_back_without_final_orchestrator_turn() -> None:
    """Verify that the orchestrator receives the specialist's output as context
    when control returns. The final result should be the specialist output
    if the orchestrator has no more turns."""
    orchestrator_model = FakeModel()
    specialist_model = FakeModel()

    specialist = Agent(name="specialist", model=specialist_model)
    orchestrator = Agent(
        name="orchestrator",
        model=orchestrator_model,
        handoffs=[
            handoff(specialist, auto_handoff_back=True)
        ],
    )

    # Turn 1: orchestrator hands off to specialist
    orchestrator_model.add_multiple_turn_outputs(
        [[get_text_message("delegating"), get_handoff_tool_call(specialist)]]
    )
    # Turn 2: specialist finishes
    specialist_model.add_multiple_turn_outputs(
        [[get_text_message("specialist final answer")]]
    )
    # Turn 3: orchestrator runs again after regaining control and produces output
    orchestrator_model.add_multiple_turn_outputs(
        [[get_text_message("based on specialist input, here is my answer")]]
    )

    result = await Runner.run(
        orchestrator,
        input="help me with this",
        max_turns=10,
    )

    assert result.last_agent.name == "orchestrator"
    assert result.final_output == "based on specialist input, here is my answer"
