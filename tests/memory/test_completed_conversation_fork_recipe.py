"""Exercise the recipes through Runner and independently owned SQLite handles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agents import Agent, ModelRequest, RunConfig, Runner, SQLiteSession
from agents.decorators import tool
from agents.items import TResponseInputItem
from agents.models.codex import CodexProvider
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.memory.completed_conversation_fork import completed_text_prefix, fork_completed
from tests.models.test_codex_provider import FakeThread, threads as threads

CONFIG = RunConfig(tracing_disabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_completed_codex_fork_preserves_final_phase_and_diverges(tmp_path, threads, streamed):
    queue, _ = threads
    original = FakeThread(text="Original")
    parent_turn = FakeThread(text="Parent branch")
    child_turn = FakeThread(text="Child branch")
    queue.extend([original, parent_turn, child_turn])
    provider = CodexProvider(cwd=tmp_path)
    config = RunConfig(model_provider=provider, tracing_disabled=True)
    agent = Agent(name="Codex assistant")
    parent = SQLiteSession("parent", tmp_path / "parent.db")
    child = None

    async def run(prompt, session):
        if streamed:
            result = Runner.run_streamed(agent, prompt, session=session, run_config=config)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, prompt, session=session, run_config=config)

    def validate_destination(items):
        provider.get_model(None).preflight(
            ModelRequest(input=items + [{"role": "user", "content": "Continue"}])
        ).raise_for_errors()

    try:
        await run("Begin", parent)
        snapshot = await parent.get_items()
        assert snapshot[-1]["phase"] == "final_answer"
        child = await fork_completed(
            parent,
            tmp_path / "child.db",
            authorize=lambda session_id, items: session_id == "parent" and items == snapshot,
            tools=set(),
            validate_destination=validate_destination,
        )
        assert await child.get_items() == snapshot
        assert (await run("Continue parent", parent)).final_output == "Parent branch"
        assert (await run("Continue child", child)).final_output == "Child branch"
        assert parent_turn.history[-1]["phase"] == "final_answer"
        assert child_turn.history[-1]["phase"] == "final_answer"
        assert (await parent.get_items())[-1]["content"][0]["text"] == "Parent branch"
        assert (await child.get_items())[-1]["content"][0]["text"] == "Child branch"
        assert all(thread.closed for thread in (original, parent_turn, child_turn))
        assert not queue
    finally:
        parent.close()
        if child is not None:
            child.close()
        await provider.aclose()


@pytest.mark.asyncio
async def test_completed_fork_diverges_without_repeating_the_write(tmp_path: Path) -> None:
    writes: list[str] = []
    workspace = tmp_path / "workspace.txt"
    workspace.write_text("existing workspace")

    @tool
    def publish_note(text: str) -> str:
        """Record a visible effect."""
        writes.append(text)
        return "Published"

    parent = SQLiteSession("parent", tmp_path / "history.db")
    child = None
    try:
        agent = Agent(
            name="Writer",
            tools=[publish_note],
            model=ScriptedModel(
                [
                    [function_call("publish_note", {"text": "one write"}, call_id="write-1")],
                    [assistant_message("Finished")],
                ]
            ),
        )
        await Runner.run(
            agent,
            "Publish",
            session=parent,
            context={"credential": "private-token"},
            run_config=CONFIG,
        )
        original = await parent.get_items()
        child = await fork_completed(
            parent,
            tmp_path / "child.db",
            authorize=lambda session_id, items: session_id == "parent" and items == original,
            tools={"publish_note"},
            validate_destination=lambda items: None,
        )
        assert await child.get_items() == original
        agent.model = ScriptedModel([[assistant_message("Parent answer")]])
        await Runner.run(agent, "Parent direction", session=parent, run_config=CONFIG)
        agent.model = ScriptedModel([[assistant_message("Child answer")]])
        await Runner.run(agent, "Child direction", session=child, run_config=CONFIG)
        parent_history, child_history = await parent.get_items(), await child.get_items()
        assert parent_history[: len(original)] == child_history[: len(original)] == original
        assert parent_history[-2].get("content") == "Parent direction"
        assert child_history[-2].get("content") == "Child direction"
        assert writes == ["one write"]
        assert "private-token" not in json.dumps(child_history)
        assert workspace.read_text() == "existing workspace"
        assert parent.session_id != child.session_id
        # No RunState, context, credentials or approvals are attached to either history store.
        assert "approval" not in json.dumps(child_history)
    finally:
        parent.close()
        if child is not None:
            child.close()


def message(role: str, content: str) -> TResponseInputItem:
    return cast(TResponseInputItem, {"role": role, "content": content})


@pytest.mark.parametrize(
    "items",
    [
        [{"type": "item_reference", "id": "server-only"}],
        [{"type": "mcp_approval_request", "id": "approval"}],
        [
            {
                "type": "function_call",
                "call_id": "pending",
                "name": "publish_note",
                "arguments": "{}",
            }
        ],
        [{"type": "function_call_output", "call_id": "orphan", "output": "effect"}],
        [
            message("user", "Input"),
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "input_file", "file_id": "private-file"}],
            },
        ],
        [
            message("user", "Input"),
            message("assistant", "done"),
            {"type": "compaction", "encrypted_content": "opaque"},
        ],
        [
            {
                "type": "function_call",
                "call_id": "pending",
                "name": "publish_note",
                "arguments": "{}",
            },
            message("assistant", "not complete"),
        ],
    ],
    ids=[
        "server-reference",
        "approval",
        "pending-tool",
        "orphan-output",
        "remote-file",
        "compaction",
        "incomplete-pair",
    ],
)
def test_fork_rejects_nonportable_and_incomplete_histories(items: list[Any]) -> None:
    with pytest.raises(ValueError):
        completed_text_prefix(items, tools={"publish_note"})


@pytest.mark.asyncio
async def test_unauthorized_export_does_not_create_destination(tmp_path: Path) -> None:
    parent = SQLiteSession("parent")
    destination = tmp_path / "rejected.db"
    try:
        await parent.add_items([message("user", "secret"), message("assistant", "done")])
        with pytest.raises(PermissionError):
            await fork_completed(
                parent,
                destination,
                authorize=lambda *_: False,
                tools=set(),
                validate_destination=lambda items: None,
            )
        assert not destination.exists()
    finally:
        parent.close()


@pytest.mark.asyncio
async def test_destination_incompatibility_fails_before_import(tmp_path: Path) -> None:
    parent = SQLiteSession("parent")
    destination = tmp_path / "incompatible.db"

    def reject_destination(items: list[TResponseInputItem]) -> None:
        raise ValueError("The destination model cannot replay this history.")

    try:
        await parent.add_items([message("user", "input"), message("assistant", "done")])
        with pytest.raises(ValueError, match="destination model"):
            await fork_completed(
                parent,
                destination,
                authorize=lambda *_: True,
                tools=set(),
                validate_destination=reject_destination,
            )
        assert not destination.exists()
    finally:
        parent.close()


def test_export_snapshot_is_deeply_independent() -> None:
    items: list[Any] = [{"role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]
    exported = cast(list[Any], completed_text_prefix(items, tools=set()))
    exported[0]["content"][0]["text"] = "changed"
    assert items[0]["content"][0]["text"] == "done"


def test_export_preserves_commentary_before_final_answer() -> None:
    items = [
        {"role": "assistant", "content": "Checking", "phase": "commentary"},
        {"role": "assistant", "content": "Finished", "phase": "final_answer"},
    ]
    assert completed_text_prefix(items, tools=set()) == items


@pytest.mark.parametrize(
    "items",
    [
        [{"role": "assistant", "content": "Checking", "phase": "commentary"}],
        [{"role": "assistant", "content": "Unknown", "phase": "unknown"}],
        [
            {"role": "user", "content": "Begin", "phase": "final_answer"},
            {"role": "assistant", "content": "Finished"},
        ],
    ],
    ids=["commentary-boundary", "unknown-phase", "user-phase"],
)
def test_export_rejects_incomplete_or_invalid_phases(items) -> None:
    with pytest.raises(ValueError):
        completed_text_prefix(items, tools=set())
