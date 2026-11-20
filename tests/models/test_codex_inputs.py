"""Exercise the provider-wire conversion boundary without starting Codex."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agents import Agent, function_tool, handoff
from agents.exceptions import UserError
from agents.items import TResponseInputItem
from agents.models._codex_inputs import (
    prepare_codex_history,
    prepare_dynamic_tools,
    sdk_tool_call,
)
from agents.tool import FileSearchTool, tool_namespace


def test_replay_preserves_phases_tool_outputs_and_compaction():
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                {"role": "user", "content": "Hello"},
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Checking", "annotations": []}],
                },
                {"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "output": ""},
                {"type": "compaction", "id": "compact_1", "encrypted_content": "opaque"},
                {"role": "user", "content": "Continue"},
                {"role": "user", "content": [{"type": "input_text", "text": "Please"}]},
            ],
        )
    )
    assert history[1]["phase"] == "commentary"
    assert history[2]["call_id"] == history[3]["call_id"] == "call_1"
    assert history[3]["output"] == ""
    assert history[4] == {"type": "compaction", "id": "compact_1", "encrypted_content": "opaque"}
    assert turn == [
        {"type": "text", "text": "Continue", "text_elements": []},
        {"type": "text", "text": "Please", "text_elements": []},
    ]


def test_tool_result_continuation_uses_history_without_fabricated_user_input():
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                {
                    "type": "function_call",
                    "name": "transfer_to_agent",
                    "call_id": "handoff_1",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "handoff_1", "output": "Transferred"},
            ],
        )
    )
    assert len(history) == 2
    assert turn == []


@pytest.mark.parametrize("local_image_format", ["path", "uri", "localhost_uri"])
def test_data_and_local_images_preserve_content_in_turn_and_tool_output(
    tmp_path, local_image_format
):
    image = tmp_path / "image %20 café#.png"
    image.write_bytes(b"image bytes")
    data = "data:image/png;base64,aW1hZ2UgYnl0ZXM="
    local_image = {
        "path": str(image),
        "uri": image.as_uri(),
        "localhost_uri": image.as_uri().replace("file:///", "file://localhost/", 1),
    }[local_image_format]
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                {"type": "function_call", "name": "capture", "call_id": "c", "arguments": "{}"},
                {
                    "type": "function_call_output",
                    "call_id": "c",
                    "output": [{"type": "input_image", "image_url": local_image}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": data, "detail": "high"}],
                },
            ],
        )
    )
    assert history[1]["output"] == [{"type": "input_image", "image_url": data}]
    assert turn == [{"type": "image", "image_url": data, "detail": "high"}]


@pytest.mark.parametrize(
    "image_url",
    [
        "file://example.com/a.png",
        "file:///%5C%5Cexample.com/share/a.png",
        r"\\example.com\share\a.png",
        r"\\?\C:\a.png",
        r"\\.\C:\a.png",
    ],
)
def test_remote_or_device_images_are_rejected_before_reading(image_url, monkeypatch):
    def unexpected_read(path):
        raise AssertionError(f"Unexpected image read: {path}")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(UserError):
        prepare_codex_history(
            [{"role": "user", "content": [{"type": "input_image", "image_url": image_url}]}]
        )


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"role": "user", "content": "  "}],
        [{"role": "system", "content": "unsupported"}],
        [
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}],
            }
        ],
        [{"role": "user", "content": [{"type": "input_file", "file_id": "file_1"}]}],
        [{"type": "function_call", "name": "f", "call_id": "c", "arguments": "{}"}],
        [{"type": "function_call_output", "call_id": "c", "output": "orphan"}],
    ],
)
def test_unsupported_history_is_rejected(items: Any):
    with pytest.raises(UserError):
        prepare_codex_history(items)


def test_tools_preserve_namespaces_handoffs_and_approval_ownership():
    @function_tool(needs_approval=True)
    def lookup(query: str) -> str:
        return query

    namespaced = tool_namespace(name="research", description="Research tools", tools=[lookup])[0]
    declarations, identities = prepare_dynamic_tools(
        [namespaced], [handoff(Agent(name="Specialist"))]
    )
    assert declarations[0] == {
        "type": "namespace",
        "name": "research",
        "description": "Research tools",
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": lookup.description,
                "inputSchema": lookup.params_json_schema,
            }
        ],
    }
    assert declarations[1]["name"] == "transfer_to_specialist"
    call = sdk_tool_call(
        {
            "callId": "call_1",
            "namespace": "research",
            "tool": "lookup",
            "arguments": {"query": "question"},
        },
        identities,
    )
    assert call.call_id == "call_1"
    assert call.model_dump()["namespace"] == "research"
    assert json.loads(call.arguments) == {"query": "question"}
    assert lookup.needs_approval is True


def test_agent_as_tool_is_declared_as_a_function():
    tool = Agent(name="Specialist").as_tool(tool_name="ask_specialist", tool_description="Ask")
    declarations, identities = prepare_dynamic_tools([tool], [])
    assert declarations[0]["type"] == "function"
    assert identities == {(None, "ask_specialist")}


def test_unsupported_tools_and_ambiguous_calls_are_rejected():
    @function_tool
    def lookup() -> str:
        return "value"

    with pytest.raises(UserError, match="direct SDK"):
        prepare_dynamic_tools([FileSearchTool(vector_store_ids=["vs_1"])], [])
    with pytest.raises(UserError, match="unique"):
        prepare_dynamic_tools([lookup, lookup], [])
    with pytest.raises(UserError, match="unrecognized"):
        sdk_tool_call(
            {"callId": "c", "namespace": None, "tool": "unexpected", "arguments": {}},
            {(None, "lookup")},
        )


def test_developer_instructions_preserve_the_stock_history_role():
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                {"role": "developer", "content": "Use metric units."},
                {"role": "user", "content": "Measure"},
            ],
        )
    )
    assert history == [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "Use metric units."}],
        }
    ]
    assert turn[0]["text"] == "Measure"


def test_deferred_tools_use_stock_namespaces_and_sdk_synthetic_routing():
    @function_tool(defer_loading=True)
    def lookup(query: str) -> str:
        return query

    from agents.tool import ToolSearchTool

    declarations, identities = prepare_dynamic_tools([lookup, ToolSearchTool()], [])
    assert declarations[0]["name"] == "lookup"
    assert declarations[0]["type"] == "namespace"
    assert declarations[0]["tools"][0]["deferLoading"] is True
    assert identities == {("lookup", "lookup")}
    call = sdk_tool_call(
        {"callId": "c", "namespace": "lookup", "tool": "lookup", "arguments": {"query": "q"}},
        identities,
    )
    assert call.model_dump()["namespace"] == "lookup"
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                {
                    "type": "tool_search_call",
                    "call_id": "s",
                    "execution": "server",
                    "arguments": {"query": "lookup"},
                    "status": "completed",
                },
                {
                    "type": "tool_search_output",
                    "call_id": "s",
                    "execution": "server",
                    "status": "completed",
                    "tools": [{"type": "function", "name": "lookup", "namespace": "lookup"}],
                },
            ],
        )
    )
    assert history[1]["tools"][0]["namespace"] == "lookup"
    assert turn == []


def test_callback_tool_calls_and_replay_keep_runner_native_types():
    from agents.tool import ShellTool

    async def execute(request):
        raise AssertionError("The model adapter must not execute callback tools.")

    skill = {"name": "audit", "description": "Inspect logs.", "path": "/fixtures/audit"}
    tool = ShellTool(
        executor=execute,
        name="my_shell",
        needs_approval=True,
        environment={"type": "local", "skills": [skill]},
    )
    declarations, identities = prepare_dynamic_tools([tool], [])
    assert declarations[0]["name"] == "agents_sdk"
    description = declarations[0]["tools"][0]["description"]
    assert skill["path"] in description and skill["description"] in description
    call = sdk_tool_call(
        {
            "callId": "c",
            "namespace": "agents_sdk",
            "tool": "my_shell",
            "arguments": {"action": {"commands": ["pwd"]}},
        },
        identities,
        [tool],
    )
    assert call.type == "shell_call"
    assert call.action.commands == ["pwd"]
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                call.model_dump(exclude_none=True),
                {
                    "type": "shell_call_output",
                    "call_id": "c",
                    "output": [
                        {
                            "stdout": "/workspace",
                            "stderr": "",
                            "outcome": {"type": "exit", "exit_code": 0},
                        }
                    ],
                },
            ],
        ),
        [tool],
    )
    assert history[0]["type"] == "function_call"
    assert history[0]["name"] == "my_shell"
    assert "/workspace" in history[1]["output"]
    assert turn == []


def test_patch_callback_tool_retains_operations_for_runner_and_replay():
    from agents.tool import ApplyPatchTool

    class Editor:
        async def create_file(self, operation):
            raise AssertionError("The model adapter must not invoke the editor.")

        async def update_file(self, operation):
            raise AssertionError("The model adapter must not invoke the editor.")

        async def delete_file(self, operation):
            raise AssertionError("The model adapter must not invoke the editor.")

    tool = ApplyPatchTool(editor=Editor(), needs_approval=True)
    declarations, identities = prepare_dynamic_tools([tool], [])
    operation = {
        "type": "update_file",
        "path": "report.txt",
        "diff": "@@\n-old\n+new",
        "move_to": "new-report.txt",
    }
    call = sdk_tool_call(
        {
            "callId": "p",
            "namespace": "agents_sdk",
            "tool": "apply_patch",
            "arguments": {"operation": operation},
        },
        identities,
        [tool],
    )
    assert declarations[0]["tools"][0]["name"] == "apply_patch"
    assert call.type == "apply_patch_call"
    assert call.operation.model_dump() == operation
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                call.model_dump(exclude_none=True),
                {
                    "type": "apply_patch_call_output",
                    "call_id": "p",
                    "status": "completed",
                    "output": "Updated",
                },
            ],
        ),
        [tool],
    )
    assert json.loads(history[0]["arguments"]) == {"operation": operation}
    assert json.loads(history[1]["output"]) == {"status": "completed", "output": "Updated"}
    assert turn == []


def test_inline_cache_breakpoints_are_rejected_before_they_can_be_dropped():
    with pytest.raises(UserError, match="cache breakpoints"):
        prepare_codex_history(
            cast(
                list[TResponseInputItem],
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Keep",
                                "prompt_cache_breakpoint": {"type": "default"},
                            }
                        ],
                    }
                ],
            )
        )


def test_reasoning_replay_preserves_encrypted_bytes_and_summary():
    item = {
        "type": "reasoning",
        "id": "reason_1",
        "summary": [{"type": "summary_text", "text": "Plan"}],
        "encrypted_content": "encrypted",
    }
    history, turn = prepare_codex_history(cast(list[TResponseInputItem], [item]))
    assert history == [item]
    assert turn == []


def test_inline_audio_preserves_exact_base64_in_history_and_latest_turn():
    import base64
    import io
    import wave

    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 160)
    encoded = base64.b64encode(data.getvalue()).decode()
    part = {"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}}
    history, turn = prepare_codex_history(
        cast(
            list[TResponseInputItem],
            [
                {"role": "user", "content": [part]},
                {"role": "assistant", "content": "Heard"},
                {"role": "user", "content": [part]},
            ],
        )
    )
    url = f"data:audio/wav;base64,{encoded}"
    assert history[0]["content"] == [{"type": "input_audio", "audio_url": url}]
    assert turn == [{"type": "audio", "audio_url": url}]
    with pytest.raises(UserError, match="bytes matching"):
        prepare_codex_history(
            cast(
                list[TResponseInputItem],
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": "YWFh", "format": "wav"},
                            }
                        ],
                    }
                ],
            )
        )


def test_web_search_uses_native_declaration_and_preserves_search_replay():
    from agents.tool import WebSearchTool

    assert prepare_dynamic_tools([WebSearchTool()], []) == ([], set())
    item = {
        "type": "web_search_call",
        "id": "search_1",
        "status": "completed",
        "action": {"type": "search", "query": "weather"},
    }
    history, turn = prepare_codex_history(cast(list[TResponseInputItem], [item]))
    assert history == [item]
    assert turn == []


@pytest.mark.parametrize("output", ["", [{"type": "input_text", "text": "patch result"}]])
def test_custom_native_pair_replay_preserves_input_and_output(output):
    call = {
        "type": "custom_tool_call",
        "id": "custom_item",
        "call_id": "custom_call",
        "name": "apply_patch",
        "input": "*** Begin Patch\n*** End Patch",
        "namespace": "functions",
    }
    history, turn = prepare_codex_history(
        [
            call,
            {"type": "custom_tool_call_output", "call_id": "custom_call", "output": output},
            {"role": "user", "content": "Continue"},
        ]
    )
    assert history == [
        {key: value for key, value in call.items() if key != "id"},
        {"type": "custom_tool_call_output", "call_id": "custom_call", "output": output},
    ]
    assert turn[0]["text"] == "Continue"


def test_native_replay_rejects_mismatched_call_output_forms():
    with pytest.raises(UserError, match="match a tool call"):
        prepare_codex_history(
            [
                {
                    "type": "function_call",
                    "call_id": "call",
                    "name": "exec_command",
                    "arguments": "{}",
                },
                {"type": "custom_tool_call_output", "call_id": "call", "output": "result"},
            ]
        )
