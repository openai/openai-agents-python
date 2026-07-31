from __future__ import annotations

from pathlib import Path

import pytest

from agents import (
    Agent,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    SQLiteSession,
    ToolCallOutputItem,
)
from agents.decorators import tool

pytestmark = pytest.mark.core


@pytest.mark.parametrize("approved", [False, True], ids=["rejected", "approved"])
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_tool_approval_survives_serialized_state_and_resume(
    integration_model: str, approved: bool, streaming: bool
) -> None:
    calls: list[str] = []

    @tool(needs_approval=True)
    def perform_action(action: str) -> str:
        """Perform the deterministic action only after explicit approval."""
        calls.append(action)
        return "completed"

    agent = Agent(
        name="Packaged approval agent",
        model=integration_model,
        instructions=(
            "Call perform_action with action='deploy'. If the tool succeeds, reply exactly "
            "APPROVED. If the tool is rejected, reply exactly REJECTED."
        ),
        tools=[perform_action],
        model_settings={"max_tokens": 384},
    )
    config = RunConfig(tracing_disabled=True)
    first: RunResult | RunResultStreaming
    resumed: RunResult | RunResultStreaming

    if streaming:
        first_stream = Runner.run_streamed(agent, "Perform the deployment.", run_config=config)
        async for _event in first_stream.stream_events():
            pass
        first = first_stream
    else:
        first = await Runner.run(agent, "Perform the deployment.", run_config=config)

    assert len(first.interruptions) == 1
    interruption = first.interruptions[0]
    assert interruption.name == "perform_action"
    state_json = first.to_state().to_json()
    restored = await RunState.from_json(agent, state_json)
    restored_interruption = restored.get_interruptions()[0]

    if approved:
        restored.approve(restored_interruption)
    else:
        restored.reject(restored_interruption, rejection_message="The operator rejected deploy.")

    if streaming:
        resumed_stream = Runner.run_streamed(agent, restored, run_config=config)
        async for _event in resumed_stream.stream_events():
            pass
        resumed = resumed_stream
    else:
        resumed = await Runner.run(agent, restored, run_config=config)

    assert resumed.final_output == ("APPROVED" if approved else "REJECTED")
    assert calls == (["deploy"] if approved else [])
    assert any(isinstance(item, ToolCallOutputItem) for item in resumed.new_items)


async def test_approval_resume_preserves_durable_sqlite_tool_history(
    integration_model: str, tmp_path: Path
) -> None:
    calls: list[str] = []

    @tool(needs_approval=True)
    def confirm_release(version: str) -> str:
        """Confirm a release after its approval decision is restored."""
        calls.append(version)
        return "approved"

    agent = Agent(
        name="Packaged durable approval agent",
        model=integration_model,
        instructions="Call confirm_release with version='1.0', then reply RELEASE_APPROVED.",
        model_settings={"max_tokens": 384},
        tools=[confirm_release],
    )
    session = SQLiteSession("packaged-approval", tmp_path / "approval.sqlite3")
    config = RunConfig(tracing_disabled=True)
    try:
        first = await Runner.run(
            agent,
            "Approve the release.",
            session=session,
            run_config=config,
        )
        restored = await RunState.from_json(agent, first.to_state().to_json())
        restored.approve(restored.get_interruptions()[0])
        resumed = await Runner.run(agent, restored, session=session, run_config=config)
        saved_items = await session.get_items()
    finally:
        session.close()

    assert calls == ["1.0"]
    assert resumed.final_output == "RELEASE_APPROVED"
    assert sum(item.get("role") == "user" for item in saved_items) == 1
    assert sum(item.get("type") == "function_call_output" for item in saved_items) == 1


async def test_parallel_tool_approvals_preserve_mixed_decisions_after_serialization(
    integration_model: str, tmp_path: Path
) -> None:
    calls: list[str] = []

    @tool(needs_approval=True)
    def approve_release(version: str) -> str:
        """Approve a deterministic release version."""
        calls.append(f"release:{version}")
        return "release-approved"

    @tool(needs_approval=True)
    def notify_customer(customer: str) -> str:
        """Notify a deterministic customer."""
        calls.append(f"customer:{customer}")
        return "customer-notified"

    agent = Agent(
        name="Packaged mixed approval agent",
        model=integration_model,
        instructions=(
            "In the same turn, call approve_release with version='1.0' and notify_customer "
            "with customer='customer-42'. After their approval decisions, reply exactly "
            "MIXED_APPROVAL_READY."
        ),
        model_settings={"max_tokens": 512, "parallel_tool_calls": True},
        tools=[approve_release, notify_customer],
    )
    session = SQLiteSession("packaged-mixed-approval", tmp_path / "mixed-approval.sqlite3")
    config = RunConfig(tracing_disabled=True)
    try:
        first = await Runner.run(
            agent, "Perform both requested actions.", session=session, run_config=config
        )
        assert len(first.interruptions) == 2
        restored = await RunState.from_json(agent, first.to_state().to_json())
        for interruption in restored.get_interruptions():
            if interruption.name == "approve_release":
                restored.approve(interruption)
            else:
                restored.reject(interruption, rejection_message="Customer notification declined.")
        resumed = await Runner.run(agent, restored, session=session, run_config=config)
        stored = await session.get_items()
    finally:
        session.close()

    assert calls == ["release:1.0"]
    assert resumed.final_output == "MIXED_APPROVAL_READY"
    assert sum(item.get("role") == "user" for item in stored) == 1
    outputs = [item for item in stored if item.get("type") == "function_call_output"]
    assert len(outputs) == 2
