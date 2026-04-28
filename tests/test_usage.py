from __future__ import annotations

import pytest
from openai.types.completion_usage import CompletionTokensDetails, PromptTokensDetails
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents import Agent, Runner
from agents.usage import RequestUsage, Usage
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


@pytest.mark.asyncio
async def test_runner_run_carries_request_usage_entries() -> None:
    """Ensure usage produced by the model propagates to RunResult context."""
    usage = Usage(
        requests=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        request_usage_entries=[
            RequestUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                input_tokens_details=InputTokensDetails(cached_tokens=0),
                output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
            )
        ],
    )
    model = FakeModel(initial_output=[get_text_message("done")])
    model.set_hardcoded_usage(usage)
    agent = Agent(name="usage-agent", model=model)

    result = await Runner.run(agent, input="hi")

    propagated = result.context_wrapper.usage
    assert propagated.requests == 1
    assert propagated.total_tokens == 15
    assert len(propagated.request_usage_entries) == 1
    entry = propagated.request_usage_entries[0]
    assert entry.input_tokens == 10
    assert entry.output_tokens == 5
    assert entry.total_tokens == 15


def test_usage_add_aggregates_all_fields():
    u1 = Usage(
        requests=1,
        input_tokens=10,
        input_tokens_details=InputTokensDetails(cached_tokens=3),
        output_tokens=20,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        total_tokens=30,
    )
    u2 = Usage(
        requests=2,
        input_tokens=7,
        input_tokens_details=InputTokensDetails(cached_tokens=4),
        output_tokens=8,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=6),
        total_tokens=15,
    )

    u1.add(u2)

    assert u1.requests == 3
    assert u1.input_tokens == 17
    assert u1.output_tokens == 28
    assert u1.total_tokens == 45
    assert u1.input_tokens_details.cached_tokens == 7
    assert u1.output_tokens_details.reasoning_tokens == 11


def test_usage_add_aggregates_with_none_values():
    u1 = Usage()
    u2 = Usage(
        requests=2,
        input_tokens=7,
        input_tokens_details=InputTokensDetails(cached_tokens=4),
        output_tokens=8,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=6),
        total_tokens=15,
    )

    u1.add(u2)

    assert u1.requests == 2
    assert u1.input_tokens == 7
    assert u1.output_tokens == 8
    assert u1.total_tokens == 15
    assert u1.input_tokens_details.cached_tokens == 4
    assert u1.output_tokens_details.reasoning_tokens == 6


def test_request_usage_creation():
    """Test that RequestUsage is created correctly."""
    request_usage = RequestUsage(
        input_tokens=100,
        output_tokens=200,
        total_tokens=300,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
    )

    assert request_usage.input_tokens == 100
    assert request_usage.output_tokens == 200
    assert request_usage.total_tokens == 300
    assert request_usage.input_tokens_details.cached_tokens == 10
    assert request_usage.output_tokens_details.reasoning_tokens == 20


def test_usage_add_preserves_single_request():
    """Test that adding a single request Usage creates an RequestUsage entry."""
    u1 = Usage()
    u2 = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )

    u1.add(u2)

    # Should preserve the request usage details
    assert len(u1.request_usage_entries) == 1
    request_usage = u1.request_usage_entries[0]
    assert request_usage.input_tokens == 100
    assert request_usage.output_tokens == 200
    assert request_usage.total_tokens == 300
    assert request_usage.input_tokens_details.cached_tokens == 10
    assert request_usage.output_tokens_details.reasoning_tokens == 20


def test_usage_add_ignores_zero_token_requests():
    """Test that zero-token requests don't create request_usage_entries."""
    u1 = Usage()
    u2 = Usage(
        requests=1,
        input_tokens=0,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=0,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=0,
    )

    u1.add(u2)

    # Should not create a request_usage_entry for zero tokens
    assert len(u1.request_usage_entries) == 0


def test_usage_add_ignores_multi_request_usage():
    """Test that multi-request Usage objects don't create request_usage_entries."""
    u1 = Usage()
    u2 = Usage(
        requests=3,  # Multiple requests
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )

    u1.add(u2)

    # Should not create a request usage entry for multi-request usage
    assert len(u1.request_usage_entries) == 0


def test_usage_add_merges_existing_request_usage_entries():
    """Test that existing request_usage_entries are merged when adding Usage objects."""
    # Create first usage with request_usage_entries
    u1 = Usage()
    u2 = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )
    u1.add(u2)

    # Create second usage with request_usage_entries
    u3 = Usage(
        requests=1,
        input_tokens=50,
        input_tokens_details=InputTokensDetails(cached_tokens=5),
        output_tokens=75,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=10),
        total_tokens=125,
    )

    u1.add(u3)

    # Should have both request_usage_entries
    assert len(u1.request_usage_entries) == 2

    # First request
    first = u1.request_usage_entries[0]
    assert first.input_tokens == 100
    assert first.output_tokens == 200
    assert first.total_tokens == 300

    # Second request
    second = u1.request_usage_entries[1]
    assert second.input_tokens == 50
    assert second.output_tokens == 75
    assert second.total_tokens == 125


def test_usage_add_with_pre_existing_request_usage_entries():
    """Test adding Usage objects that already have request_usage_entries."""
    u1 = Usage()

    # Create a usage with request_usage_entries
    u2 = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )
    u1.add(u2)

    # Create another usage with request_usage_entries
    u3 = Usage(
        requests=1,
        input_tokens=50,
        input_tokens_details=InputTokensDetails(cached_tokens=5),
        output_tokens=75,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=10),
        total_tokens=125,
    )

    # Add u3 to u1
    u1.add(u3)

    # Should have both request_usage_entries
    assert len(u1.request_usage_entries) == 2
    assert u1.request_usage_entries[0].input_tokens == 100
    assert u1.request_usage_entries[1].input_tokens == 50


def test_usage_request_usage_entries_default_empty():
    """Test that request_usage_entries defaults to an empty list."""
    u = Usage()
    assert u.request_usage_entries == []


def test_anthropic_cost_calculation_scenario():
    """Test a realistic scenario for Sonnet 4.5 cost calculation with 200K token thresholds."""
    # Simulate 3 API calls: 100K, 150K, and 80K input tokens each
    # None exceed 200K, so they should all use the lower pricing tier

    usage = Usage()

    # First request: 100K input tokens
    req1 = Usage(
        requests=1,
        input_tokens=100_000,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=50_000,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=150_000,
    )
    usage.add(req1)

    # Second request: 150K input tokens
    req2 = Usage(
        requests=1,
        input_tokens=150_000,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=75_000,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=225_000,
    )
    usage.add(req2)

    # Third request: 80K input tokens
    req3 = Usage(
        requests=1,
        input_tokens=80_000,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=40_000,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=120_000,
    )
    usage.add(req3)

    # Verify aggregated totals
    assert usage.requests == 3
    assert usage.input_tokens == 330_000  # 100K + 150K + 80K
    assert usage.output_tokens == 165_000  # 50K + 75K + 40K
    assert usage.total_tokens == 495_000  # 150K + 225K + 120K

    # Verify request_usage_entries preservation
    assert len(usage.request_usage_entries) == 3
    assert usage.request_usage_entries[0].input_tokens == 100_000
    assert usage.request_usage_entries[1].input_tokens == 150_000
    assert usage.request_usage_entries[2].input_tokens == 80_000

    # All request_usage_entries are under 200K threshold
    for req in usage.request_usage_entries:
        assert req.input_tokens < 200_000
        assert req.output_tokens < 200_000


def test_usage_normalizes_none_token_details():
    # Some providers don't populate optional token detail fields
    # (cached_tokens, reasoning_tokens), and the OpenAI SDK's generated
    # code can bypass Pydantic validation (e.g., via model_construct),
    # allowing None values. We normalize these to 0 to prevent TypeErrors.

    # Test entire objects being None (BeforeValidator)
    usage = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=None,  # type: ignore[arg-type]
        output_tokens=50,
        output_tokens_details=None,  # type: ignore[arg-type]
        total_tokens=150,
    )
    assert usage.input_tokens_details.cached_tokens == 0
    assert usage.output_tokens_details.reasoning_tokens == 0

    # Test fields within objects being None (__post_init__)
    input_details = InputTokensDetails(cached_tokens=0)
    input_details.__dict__["cached_tokens"] = None

    output_details = OutputTokensDetails(reasoning_tokens=0)
    output_details.__dict__["reasoning_tokens"] = None

    usage = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=input_details,
        output_tokens=50,
        output_tokens_details=output_details,
        total_tokens=150,
    )

    # __post_init__ should normalize None to 0
    assert usage.input_tokens_details.cached_tokens == 0
    assert usage.output_tokens_details.reasoning_tokens == 0


def test_usage_normalizes_chat_completions_types():
    # Chat Completions API uses PromptTokensDetails and CompletionTokensDetails,
    # while Usage expects InputTokensDetails and OutputTokensDetails (Responses API).
    # The BeforeValidator should convert between these types.

    prompt_details = PromptTokensDetails(audio_tokens=10, cached_tokens=50)
    completion_details = CompletionTokensDetails(
        accepted_prediction_tokens=5,
        audio_tokens=10,
        reasoning_tokens=100,
        rejected_prediction_tokens=2,
    )

    usage = Usage(
        requests=1,
        input_tokens=200,
        input_tokens_details=prompt_details,  # type: ignore[arg-type]
        output_tokens=150,
        output_tokens_details=completion_details,  # type: ignore[arg-type]
        total_tokens=350,
    )

    # Should convert to Responses API types, extracting the relevant fields
    assert isinstance(usage.input_tokens_details, InputTokensDetails)
    assert usage.input_tokens_details.cached_tokens == 50

    assert isinstance(usage.output_tokens_details, OutputTokensDetails)
    assert usage.output_tokens_details.reasoning_tokens == 100


# ============================================================================
# Tests for agent_name and model_name on RequestUsage (issue #2100)
# ============================================================================


def test_request_usage_default_agent_model_names_are_none():
    """Backward-compat: RequestUsage without agent_name/model_name defaults to None."""
    entry = RequestUsage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    assert entry.agent_name is None
    assert entry.model_name is None


def test_serialize_deserialize_roundtrip_preserves_agent_and_model_names():
    """JSON round-trip must preserve agent_name and model_name on each entry.

    This guards against a regression where serialize_usage drops the new
    attribution fields, or deserialize_usage forgets to read them back.
    Both branches of the conditional emit (entry-with-name and entry-without-name)
    are exercised so the all-None fast path can't silently strip the keys.
    """
    from agents.usage import deserialize_usage, serialize_usage

    named_entry = RequestUsage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        agent_name="Math Tutor",
        model_name="gpt-4o",
    )
    unnamed_entry = RequestUsage(
        input_tokens=2,
        output_tokens=1,
        total_tokens=3,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    original = Usage(
        requests=2,
        input_tokens=12,
        output_tokens=6,
        total_tokens=18,
        request_usage_entries=[named_entry, unnamed_entry],
    )

    restored = deserialize_usage(serialize_usage(original))

    assert len(restored.request_usage_entries) == 2
    restored_named = restored.request_usage_entries[0]
    restored_unnamed = restored.request_usage_entries[1]

    assert restored_named.agent_name == "Math Tutor"
    assert restored_named.model_name == "gpt-4o"
    assert restored_named.input_tokens == 10
    assert restored_named.output_tokens == 5

    assert restored_unnamed.agent_name is None
    assert restored_unnamed.model_name is None
    assert restored_unnamed.input_tokens == 2


def test_request_usage_with_agent_and_model_names():
    """RequestUsage can be created with explicit agent_name and model_name."""
    entry = RequestUsage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        agent_name="Math Tutor",
        model_name="gpt-4o",
    )
    assert entry.agent_name == "Math Tutor"
    assert entry.model_name == "gpt-4o"


def test_usage_add_propagates_agent_and_model_names():
    """Usage.add() with agent_name/model_name annotates the RequestUsage entry."""
    parent = Usage()
    child = Usage(
        requests=1,
        input_tokens=65,
        output_tokens=13,
        total_tokens=78,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    parent.add(child, agent_name="Code Reviewer", model_name="gpt-4o-mini")

    assert len(parent.request_usage_entries) == 1
    entry = parent.request_usage_entries[0]
    assert entry.agent_name == "Code Reviewer"
    assert entry.model_name == "gpt-4o-mini"
    assert entry.input_tokens == 65
    assert entry.output_tokens == 13


def test_usage_add_without_agent_model_names_stays_none():
    """Usage.add() without agent/model names leaves them as None (backward compat)."""
    parent = Usage()
    child = Usage(
        requests=1,
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    parent.add(child)

    assert len(parent.request_usage_entries) == 1
    entry = parent.request_usage_entries[0]
    assert entry.agent_name is None
    assert entry.model_name is None


def test_usage_add_single_request_preserves_prebuilt_entry_attribution():
    """Single-request Usage with request_usage_entries keeps agent/model when add() has no kwargs."""
    inner = RequestUsage(
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        agent_name="Prior Run Agent",
        model_name="prior-model",
    )
    child = Usage(
        requests=1,
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        request_usage_entries=[inner],
    )
    parent = Usage()
    parent.add(child)

    assert len(parent.request_usage_entries) == 1
    out = parent.request_usage_entries[0]
    assert out.agent_name == "Prior Run Agent"
    assert out.model_name == "prior-model"


def test_usage_add_merge_existing_entries_applies_agent_model_names():
    """When merging existing request_usage_entries, agent/model names are applied to unset ones."""
    # An existing entry without names
    existing_entry = RequestUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    parent = Usage()
    child = Usage(
        requests=2,  # not 1, so it won't auto-create a new entry
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        request_usage_entries=[existing_entry],
    )
    parent.add(child, agent_name="Triage Agent", model_name="gpt-4o")

    assert len(parent.request_usage_entries) == 1
    assert parent.request_usage_entries[0].agent_name == "Triage Agent"
    assert parent.request_usage_entries[0].model_name == "gpt-4o"


def test_usage_add_merge_existing_entries_does_not_overwrite_names():
    """Existing agent/model names on entries are not overwritten during merge."""
    existing_entry = RequestUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        agent_name="Already Named Agent",
        model_name="already-named-model",
    )
    parent = Usage()
    child = Usage(
        requests=2,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        request_usage_entries=[existing_entry],
    )
    parent.add(child, agent_name="New Agent Name", model_name="new-model")

    # The existing names should NOT be overwritten
    assert parent.request_usage_entries[0].agent_name == "Already Named Agent"
    assert parent.request_usage_entries[0].model_name == "already-named-model"


@pytest.mark.asyncio
async def test_runner_run_populates_agent_name_in_request_usage():
    """Integration: Running an agent populates agent_name in RequestUsage entries."""
    from agents.usage import Usage as AgentUsage

    model_usage = AgentUsage(
        requests=1,
        input_tokens=42,
        output_tokens=8,
        total_tokens=50,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    fake = FakeModel(initial_output=[get_text_message("hello")])
    fake.set_hardcoded_usage(model_usage)
    agent = Agent(name="My Assistant", model=fake)

    result = await Runner.run(agent, input="hi")

    entries = result.context_wrapper.usage.request_usage_entries
    assert len(entries) == 1
    assert entries[0].agent_name == "My Assistant"


@pytest.mark.asyncio
async def test_runner_run_populates_model_name_in_request_usage():
    """Integration: Running an agent populates model_name in RequestUsage entries."""
    from agents.usage import Usage as AgentUsage

    model_usage = AgentUsage(
        requests=1,
        input_tokens=30,
        output_tokens=10,
        total_tokens=40,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    # FakeModel doesn't expose a `.model` attribute by default → model_name should be None
    # We give it one to test that model_name is picked up.
    fake = FakeModel(initial_output=[get_text_message("ok")])
    fake.model = "test-model-name"  # type: ignore[attr-defined]
    fake.set_hardcoded_usage(model_usage)
    agent = Agent(name="Model-Aware Agent", model=fake)

    result = await Runner.run(agent, input="ping")

    entries = result.context_wrapper.usage.request_usage_entries
    assert len(entries) == 1
    assert entries[0].model_name == "test-model-name"


@pytest.mark.asyncio
async def test_multi_agent_run_attributes_usage_to_correct_agents():
    """Multi-agent scenario: each RequestUsage entry has the right agent_name."""

    from agents.usage import Usage as AgentUsage
    from tests.test_responses import get_handoff_tool_call

    # Two separate models so we can track which agent's usage is which
    triage_usage = AgentUsage(
        requests=1,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    specialist_usage = AgentUsage(
        requests=1,
        input_tokens=200,
        output_tokens=20,
        total_tokens=220,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )

    specialist_model = FakeModel(initial_output=[get_text_message("specialist done")])
    specialist_model.model = "gpt-4o-specialist"  # type: ignore[attr-defined]
    specialist_model.set_hardcoded_usage(specialist_usage)
    specialist_agent = Agent(name="Specialist Agent", model=specialist_model)

    triage_model = FakeModel()
    triage_model.model = "gpt-4o-triage"  # type: ignore[attr-defined]
    triage_model.add_multiple_turn_outputs(
        [
            [get_handoff_tool_call(specialist_agent)],
        ]
    )
    triage_model.set_hardcoded_usage(triage_usage)
    triage_agent = Agent(name="Triage Agent", model=triage_model, handoffs=[specialist_agent])

    result = await Runner.run(triage_agent, input="route me")

    all_entries = result.context_wrapper.usage.request_usage_entries
    assert len(all_entries) == 2, f"Expected 2 request entries, got {len(all_entries)}"

    agent_names = [e.agent_name for e in all_entries]
    assert "Triage Agent" in agent_names, f"Expected 'Triage Agent' in {agent_names}"
    assert "Specialist Agent" in agent_names, f"Expected 'Specialist Agent' in {agent_names}"

    triage_entry = next(e for e in all_entries if e.agent_name == "Triage Agent")
    assert triage_entry.input_tokens == 100
    assert triage_entry.model_name == "gpt-4o-triage", (
        f"Triage entry model_name should be 'gpt-4o-triage', got {triage_entry.model_name!r}"
    )

    specialist_entry = next(e for e in all_entries if e.agent_name == "Specialist Agent")
    assert specialist_entry.input_tokens == 200
    assert specialist_entry.model_name == "gpt-4o-specialist", (
        "Specialist entry model_name should be 'gpt-4o-specialist', "
        f"got {specialist_entry.model_name!r}"
    )


def test_add_does_not_mutate_other_entries() -> None:
    """Adding a Usage with existing request_usage_entries must not mutate the original entries.

    Previously, the elif branch in Usage.add() called entry.agent_name = ... directly on
    the objects inside other.request_usage_entries, causing silent mis-attribution when the
    same Usage object was re-used or added to multiple aggregators.
    """
    from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

    source_entry = RequestUsage(
        input_tokens=50,
        output_tokens=25,
        total_tokens=75,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        agent_name=None,
        model_name=None,
    )

    # Build a Usage that already has request_usage_entries (requests != 1 path)
    other = Usage(
        requests=2,
        input_tokens=50,
        output_tokens=25,
        total_tokens=75,
        request_usage_entries=[source_entry],
    )

    agg = Usage()
    agg.add(other, agent_name="MyAgent", model_name="gpt-4o")

    # The aggregator should have a copy with the annotation applied
    assert len(agg.request_usage_entries) == 1
    assert agg.request_usage_entries[0].agent_name == "MyAgent"
    assert agg.request_usage_entries[0].model_name == "gpt-4o"

    # The original entry must NOT be mutated
    assert source_entry.agent_name is None, "Original entry was mutated!"
    assert source_entry.model_name is None, "Original entry was mutated!"
