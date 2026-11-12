"""Accepted tool history has been remaining data during native memory generation."""

import json
import os
from pathlib import Path

import pytest

from _agents_codex.history import CanonicalHistorySnapshot
from _agents_codex.sandbox_memory import _memory_transcript
from agents import Agent, ToolOutput, function_tool
from agents.sandbox import Manifest, SandboxMemoryOptions, sandbox_environment
from agents.testing import ScriptedResponses, text, tool_call
from tests._asyncio import timeout
from tests.test_native_sandbox_memory import _workspace_gateway

pytestmark = pytest.mark.skipif(os.name == "nt", reason="UnixLocal memory fixture")


@pytest.mark.parametrize("failed", [False, True], ids=["completed-tool", "failed-tool"])
async def test_memory_flush_retains_the_source_tool_exchange_without_replaying_it(
    tmp_path: Path, monkeypatch, failed: bool
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    workspace = tmp_path / "workspace"
    effects = []

    @function_tool
    def lookup_preference(subject: str) -> str | ToolOutput:
        effects.append(subject)
        if failed:
            return ToolOutput.error("SYNTHETIC_TOOL_RESULT: preference unavailable")
        return "SYNTHETIC_TOOL_RESULT: plain language"

    extraction = {
        "rollout_slug": "tool-preference",
        "rollout_summary": "The tool returned a language preference.",
        "raw_memory": "Prefer plain language.",
    }
    consolidation = {"summary": "Use plain language.", "memory": "Prefer plain language."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [tool_call("lookup_preference", {"subject": "tone"}, call_id="source-tool")],
            [text("Preference checked.")],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ) as peer,
    ):
        options = SandboxMemoryOptions(
            phase_one=Agent(instructions="Extract memory.", provider=peer.provider),
            phase_two=Agent(instructions="Consolidate memory.", provider=peer.provider),
        )
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
            memory=options,
        )
        agent = Agent(
            instructions="PRIVATE_SOURCE_INSTRUCTION",
            provider=peer.provider,
            tools=[lookup_preference],
        )
        async with agent.session(environment=environment) as session:
            history = await session.history_snapshot()
            await session.transact_history(
                expected_revision=history.revision,
                keep=history.total,
                items=[
                    {
                        "type": "application_note",
                        "role": "user",
                        "value": "PRIVATE_APPLICATION_NOTE",
                    },
                    {
                        "type": "function_call",
                        "name": "lookup_preference",
                        "arguments": '{"subject":"previous"}',
                        "call_id": "imported-tool",
                        "role": "developer",
                        "audit_metadata": "PRIVATE_IMPORTED_TOOL",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "imported-tool",
                        "role": "system",
                        "output": [
                            {
                                "type": "input_text",
                                "text": "Imported answer",
                                "audit_metadata": "PRIVATE_IMPORTED_TOOL",
                            }
                        ],
                        "audit_metadata": "PRIVATE_IMPORTED_TOOL",
                    },
                ],
            )
            originals = json.dumps((await session.history_snapshot()).items)
            assert "PRIVATE_APPLICATION_NOTE" in originals
            assert "PRIVATE_IMPORTED_TOOL" in originals
            turn = await session.submit("Check and remember my tone preference.")
            assert (await turn.get_result()).final_output == "Preference checked."
            assert effects == ["tone"]
            await session.flush_memory()
            await session.flush_memory()
            lines = (workspace / "sessions" / f"{session.id}.jsonl").read_text().splitlines()
            assert len(lines) == 1
            rollout = json.loads(lines[0])
            items = rollout["items"]
            assert [item.get("type", "message") for item in items] == [
                "function_call",
                "function_call_output",
                "message",
                "function_call",
                "function_call_output",
                "message",
            ]
            assert items[0]["call_id"] == items[1]["call_id"] == "imported-tool"
            assert items[1]["output"] == [{"type": "input_text", "text": "Imported answer"}]
            assert items[3]["name"] == "lookup_preference"
            assert items[3]["call_id"] == items[4]["call_id"] == "source-tool"
            assert json.loads(items[3]["arguments"]) == {"subject": "tone"}
            assert "role" not in items[0] and "role" not in items[1]
            result = items[4].get("error", items[4].get("output"))
            expected_result = "preference unavailable" if failed else "plain language"
            assert expected_result in json.dumps(result)
            assert rollout["terminal_metadata"]["terminal_state"] == "completed"
            assert "PRIVATE_SOURCE_INSTRUCTION" not in lines[0]
            assert "PRIVATE_APPLICATION_NOTE" not in lines[0]
            assert "PRIVATE_IMPORTED_TOOL" not in lines[0]
            assert "Current SDK workspace snapshot" not in lines[0]
            assert (workspace / "memories" / "MEMORY.md").read_text() == consolidation["memory"]
            assert effects == ["tone"]
            assert len(peer.requests) == 4
            assert "PRIVATE_APPLICATION_NOTE" not in json.dumps(peer.requests[0]["input"])
            phase_one_input = json.dumps(peer.requests[2]["input"])
            assert "source-tool" in phase_one_input
            assert "SYNTHETIC_TOOL_RESULT" in phase_one_input
            assert "PRIVATE_SOURCE_INSTRUCTION" not in phase_one_input
            assert "PRIVATE_APPLICATION_NOTE" not in phase_one_input
            assert "PRIVATE_IMPORTED_TOOL" not in phase_one_input


def test_memory_preserves_projected_failed_function_output() -> None:
    snapshot = CanonicalHistorySnapshot(
        revision="test-revision",
        total=1,
        items=(
            {
                "type": "function_call_output",
                "call_id": "failed-tool",
                "status": "failed",
                "output": None,
                "error": "Tool timed out",
                "audit_metadata": "PRIVATE_IMPORTED_TOOL",
            },
        ),
    )
    assert _memory_transcript("native-thread", snapshot, "sandbox")["items"] == [
        {
            "type": "function_call_output",
            "call_id": "failed-tool",
            "status": "failed",
            "error": "Tool timed out",
        }
    ]


@pytest.mark.parametrize("kind", ["file_search_call", "code_interpreter_call"])
def test_memory_refuses_unprojected_hosted_tool_history(kind: str) -> None:
    snapshot = CanonicalHistorySnapshot(
        revision="test-revision",
        total=1,
        items=({"type": kind, "id": "hosted-call", "status": "completed"},),
    )
    with pytest.raises(ValueError, match="requires text messages and function tool history"):
        _memory_transcript("native-thread", snapshot, "sandbox")
