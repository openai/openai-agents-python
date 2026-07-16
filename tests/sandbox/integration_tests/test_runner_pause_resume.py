from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from agents import RunConfig, Runner, function_tool
from agents.items import RunItem, ToolCallOutputItem
from agents.run_state import RunState
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session import CallbackSink, Instrumentation, SandboxSessionEvent
from tests.sandbox.integration_tests._helpers import (
    SandboxFileCapability,
    SandboxLifecycleProbeCapability,
    build_manifest_with_all_entry_types,
    create_local_sources,
    install_mock_external_tools,
)
from tests.sandbox.integration_tests.test_model import TestModel


@pytest.mark.asyncio
async def test_runner_preserves_unix_local_lifecycle_state_across_pause_and_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_mock_external_tools(monkeypatch, tmp_path)
    source_root = create_local_sources(tmp_path)
    manifest = build_manifest_with_all_entry_types(
        workspace_root=Path("/workspace"),
        source_root=source_root,
    )
    events: list[SandboxSessionEvent] = []
    client = UnixLocalSandboxClient(
        instrumentation=Instrumentation(
            sinks=[CallbackSink(lambda event, _session: events.append(event), mode="sync")]
        )
    )
    model = TestModel()
    model.queue_function_call(
        "assert_manifest_materialized",
        {},
        call_id="call_manifest_materialized",
    )
    model.queue_function_call(
        "write_file",
        {"path": "runtime_note.txt", "content": "runtime note v1\n"},
        call_id="call_write_runtime_note",
    )
    model.queue_function_call(
        "apply_lifecycle_patch",
        {},
        call_id="call_apply_lifecycle_patch",
    )
    model.queue_function_call(
        "assert_workspace_escape_blocked",
        {},
        call_id="call_assert_workspace_escape_blocked",
    )
    model.queue_function_call(
        "extract_lifecycle_archive",
        {},
        call_id="call_extract_lifecycle_archive",
    )
    model.queue_function_call(
        "start_lifecycle_pty",
        {},
        call_id="call_start_lifecycle_pty",
    )
    model.queue_function_call("approval_tool", {}, call_id="call_approval")

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Use the sandbox lifecycle tools.",
        default_manifest=manifest,
        tools=[approval_tool],
        capabilities=[SandboxFileCapability(), SandboxLifecycleProbeCapability()],
    )

    first_run = await Runner.run(
        agent,
        "verify the UnixLocal sandbox lifecycle and wait for approval",
        run_config=RunConfig(sandbox=SandboxRunConfig(client=client)),
    )

    assert _tool_outputs(first_run.new_items, agent=agent) == [
        "manifest materialized",
        "wrote runtime_note.txt",
        "lifecycle patch applied",
        "workspace escape blocked",
        "archive extracted",
        "pty started and echoed stdin",
    ]
    assert len(first_run.interruptions) == 1
    state = first_run.to_state()
    assert state._sandbox is not None
    assert state._sandbox["backend_id"] == "unix_local"
    assert state._sandbox["current_agent_name"] == "sandbox"
    session_state = state._sandbox["session_state"]
    assert isinstance(session_state, dict)
    snapshot = session_state["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["type"] == "local"
    assert session_state["workspace_root_owned"] is True
    assert session_state["workspace_root_ready"] is True
    workspace_root = _session_state_manifest_root(session_state)
    assert not workspace_root.exists()
    assert _successful_event_count(events, op="stop") == 1
    assert _successful_event_count(events, op="shutdown") == 1

    resumed_model = TestModel()
    resumed_model.queue_function_call(
        "assert_restored_lifecycle_state",
        {},
        call_id="call_assert_restored_lifecycle_state",
    )
    resumed_model.queue_function_call(
        "read_file",
        {"path": "runtime_note.txt"},
        call_id="call_read_runtime_note",
    )
    resumed_model.queue_final_output("done")
    resumed_agent = SandboxAgent(
        name="sandbox",
        model=resumed_model,
        instructions="Use the sandbox lifecycle tools.",
        default_manifest=manifest,
        tools=[approval_tool],
        capabilities=[SandboxFileCapability(), SandboxLifecycleProbeCapability()],
    )

    restored_state = await RunState.from_json(resumed_agent, state.to_json())
    restored_interruptions = restored_state.get_interruptions()
    assert len(restored_interruptions) == 1
    restored_state.approve(restored_interruptions[0])

    resumed = await Runner.run(
        resumed_agent,
        restored_state,
        run_config=RunConfig(sandbox=SandboxRunConfig(client=client)),
    )

    assert resumed.final_output == "done"
    assert not workspace_root.exists()
    assert _successful_event_count(events, op="stop") == 2
    assert _successful_event_count(events, op="shutdown") == 2
    assert _tool_outputs(resumed.new_items, agent=resumed_agent)[-3:] == [
        "approved",
        "restored lifecycle state verified",
        "runtime note v1\n",
    ]


def _session_state_manifest_root(session_state: dict[str, object]) -> Path:
    manifest = session_state["manifest"]
    assert isinstance(manifest, dict)
    root = manifest["root"]
    assert isinstance(root, str)
    return Path(root)


def _successful_event_count(events: list[SandboxSessionEvent], *, op: str) -> int:
    return sum(
        1
        for event in events
        if event.op == op and event.phase == "finish" and getattr(event, "ok", False) is True
    )


def _tool_outputs(items: Sequence[RunItem], *, agent: SandboxAgent) -> list[str]:
    outputs: list[str] = []
    for item in items:
        if isinstance(item, ToolCallOutputItem) and item.agent is agent:
            assert isinstance(item.output, str)
            outputs.append(item.output)
    return outputs
