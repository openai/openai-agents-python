"""Accepted tool history has been remaining data during native memory generation."""

import json
import os
from pathlib import Path

import pytest

from _agents_codex.history import CanonicalHistorySnapshot
from _agents_codex.sandbox_memory import _memory_transcript
from agents import Agent, ToolOutput, ToolOutputFile, ToolOutputImage, ToolOutputText, function_tool
from agents.sandbox import Manifest, SandboxMemoryOptions, sandbox_environment
from agents.testing import ScriptedResponses, event, text, tool_call
from tests._asyncio import timeout
from tests.test_native_sandbox_memory import _workspace_gateway

pytestmark = pytest.mark.skipif(os.name == "nt", reason="UnixLocal memory fixture")


@pytest.mark.parametrize("kind", ["completed", "failed", "media", "media_optin"])
async def test_memory_flush_retains_the_source_tool_exchange_without_replaying_it(
    tmp_path: Path, monkeypatch, kind: str
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
        if kind == "failed":
            return ToolOutput.error("SYNTHETIC_TOOL_RESULT: preference unavailable")
        if kind in ("media", "media_optin"):
            return ToolOutput(
                [
                    ToolOutputText("SYNTHETIC_TOOL_RESULT: plain language"),
                    ToolOutputImage(file_id="private-tool-image-id", detail="low"),
                    ToolOutputFile(
                        file_data="data:text/plain;base64,cHJpdmF0ZQ==",
                        filename="private-tool-output.txt",
                    ),
                ]
            )
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
            **({"include_media_content": True} if kind == "media_optin" else {}),
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
            model="gpt-5.5",
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
                "message",
                "function_call",
                "function_call_output",
                "message",
            ]
            assert items[1]["name"] == "lookup_preference"
            assert items[1]["call_id"] == items[2]["call_id"] == "source-tool"
            assert json.loads(items[1]["arguments"]) == {"subject": "tone"}
            result = items[2].get("error", items[2].get("output"))
            expected_result = "preference unavailable" if kind == "failed" else "plain language"
            assert expected_result in json.dumps(result)
            if kind in ("media", "media_optin"):
                assert result == [
                    {"type": "input_text", "text": "SYNTHETIC_TOOL_RESULT: plain language"},
                    *(
                        [
                            {
                                "type": "input_image",
                                "file_id": "private-tool-image-id",
                                "detail": "low",
                            },
                            {
                                "type": "input_file",
                                "file_data": "data:text/plain;base64,cHJpdmF0ZQ==",
                                "filename": "private-tool-output.txt",
                            },
                        ]
                        if kind == "media_optin"
                        else [{"type": "input_image"}, {"type": "input_file"}]
                    ),
                ]
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
            for private in ("private-tool-image-id", "cHJpdmF0ZQ==", "private-tool-output.txt"):
                assert (private in lines[0]) == (kind == "media_optin")
                assert (private in phase_one_input) == (kind == "media_optin")


@pytest.mark.parametrize("include_media", [False, True])
def test_memory_media_projection_requires_opt_in_and_drops_unknown_metadata(include_media):
    parts = [
        {
            "type": "input_image",
            "image_url": "https://synthetic.invalid/private",
            "extra": "PRIVATE_METADATA",
        },
        {
            "type": "input_file",
            "file_data": "SYNTHETIC_FILE_DATA",
            "filename": "synthetic.txt",
            "extra": "PRIVATE_METADATA",
        },
        {
            "type": "input_audio",
            "audio_url": "https://synthetic.invalid/SYNTHETIC_AUDIO_DATA",
            "extra": "PRIVATE_METADATA",
        },
    ]
    snapshot = CanonicalHistorySnapshot(
        revision="media-projection",
        total=1,
        items=({"type": "function_call_output", "call_id": "media-tool", "output": parts},),
    )
    payload = _memory_transcript(
        "native-thread", snapshot, "sandbox", include_media_content=include_media
    )
    encoded = json.dumps(payload)
    assert "PRIVATE_METADATA" not in encoded
    for value in (
        "https://synthetic.invalid/private",
        "SYNTHETIC_FILE_DATA",
        "synthetic.txt",
        "SYNTHETIC_AUDIO_DATA",
    ):
        assert (value in encoded) is include_media
    assert [part["type"] for part in payload["items"][0]["output"]] == [
        "input_image",
        "input_file",
        "input_audio",
    ]


@pytest.mark.parametrize("include_media", [False, True])
@pytest.mark.parametrize("screenshot", [False, True])
def test_memory_hosted_media_uses_the_same_content_policy(include_media, screenshot):
    marker = "https://synthetic.invalid/SYNTHETIC_HOSTED_IMAGE"
    if screenshot:
        record = {
            "type": "computer_call_output",
            "call_id": "computer-source",
            "output": {
                "type": "computer_screenshot",
                "image_url": marker,
                "extra": "PRIVATE_METADATA",
            },
        }
    else:
        record = {
            "type": "code_interpreter_call",
            "id": "code-source",
            "status": "completed",
            "outputs": [
                {"type": "logs", "logs": "SYNTHETIC_LOG"},
                {"type": "image", "url": marker, "extra": "PRIVATE_METADATA"},
            ],
        }
    snapshot = CanonicalHistorySnapshot(revision="hosted-media", total=1, items=(record,))
    payload = _memory_transcript("thread", snapshot, "sandbox", include_media_content=include_media)
    encoded = json.dumps(payload)
    assert (marker in encoded) is include_media
    assert "PRIVATE_METADATA" not in encoded
    if not screenshot:
        assert "SYNTHETIC_LOG" in encoded


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


@pytest.mark.parametrize("kind", ["file_search_call", "code_interpreter_call", "mcp_list_tools"])
def test_memory_preserves_known_hosted_tool_history(kind: str) -> None:
    snapshot = CanonicalHistorySnapshot(
        revision="test-revision",
        total=1,
        items=({"type": kind, "id": "hosted-call", "status": "completed"},),
    )
    assert _memory_transcript("native-thread", snapshot, "sandbox")["items"] == list(snapshot.items)


@pytest.mark.parametrize("include_media", [False, True])
async def test_memory_retains_admitted_user_media_and_known_records(
    tmp_path, monkeypatch, include_media
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    workspace = tmp_path / "workspace"
    extraction = {
        "rollout_slug": "media",
        "rollout_summary": "Image context.",
        "raw_memory": "Image context.",
    }
    consolidation = {"summary": "Image context.", "memory": "Image context."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [
                event(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "file_search_call",
                            "id": "synthetic-search",
                            "status": "completed",
                            "queries": ["synthetic-query"],
                            "results": None,
                        },
                    }
                ),
                event(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "code_interpreter_call",
                            "id": "synthetic-code",
                            "status": "completed",
                            "container_id": "synthetic-container",
                            "code": "print('synthetic')",
                            "outputs": [
                                {"type": "logs", "logs": "SYNTHETIC_LOG"},
                                {
                                    "type": "image",
                                    "url": "https://synthetic.invalid/SYNTHETIC_HOSTED_IMAGE",
                                    "extra": "PRIVATE_MEDIA_METADATA",
                                },
                            ],
                        },
                    }
                ),
                event(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "image_generation_call",
                            "id": "synthetic-generation",
                            "status": "failed",
                            "result": None,
                        },
                    }
                ),
                text("Image accepted."),
            ],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ) as peer,
    ):
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
                include_media_content=include_media,
            ),
        )
        async with Agent(instructions="Describe.", model="gpt-5.5", provider=peer.provider).session(
            environment=environment
        ) as session:
            before = await session.history_snapshot()
            await session.transact_history(
                expected_revision=before.revision,
                keep=before.total,
                items=[
                    {
                        "type": "file_search_call",
                        "id": "synthetic-search",
                        "status": "completed",
                        "queries": ["synthetic-query"],
                        "results": [],
                    },
                    {"type": "application_note", "value": "SYNTHETIC_OPAQUE_AUDIT_ONLY"},
                    {
                        "type": "image_generation_call",
                        "id": "synthetic-generation",
                        "status": "completed",
                        "result": "SYNTHETIC_GENERATED_IMAGE",
                    },
                ],
            )
            await (
                await session.submit(
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "SYNTHETIC_ADMITTED_IMAGE_CONTEXT"},
                            {
                                "type": "input_image",
                                "file_id": "file-synthetic-memory-image",
                                "detail": "low",
                            },
                        ],
                    }
                )
            ).get_result()
            await session.flush_memory()
            await session.flush_memory()
            lines = (workspace / "sessions" / f"{session.id}.jsonl").read_text().splitlines()
            assert len(lines) == 1
            payload = json.loads(lines[0])
            assert any(item.get("type") == "file_search_call" for item in payload["items"])
            assert ("file-synthetic-memory-image" in lines[0]) is include_media
            assert ("SYNTHETIC_HOSTED_IMAGE" in lines[0]) is include_media
            assert "SYNTHETIC_LOG" in lines[0]
            assert "PRIVATE_MEDIA_METADATA" not in lines[0]
            assert "SYNTHETIC_OPAQUE_AUDIT_ONLY" not in lines[0]
            assert "SYNTHETIC_GENERATED_IMAGE" not in lines[0]
            extract_input = json.dumps(peer.requests[1]["input"])
            assert ("file-synthetic-memory-image" in extract_input) is include_media
            assert ("SYNTHETIC_HOSTED_IMAGE" in extract_input) is include_media
            assert "PRIVATE_MEDIA_METADATA" not in extract_input
            assert "synthetic-query" in extract_input
            assert "SYNTHETIC_OPAQUE_AUDIT_ONLY" not in extract_input
            assert len(peer.requests) == 3
