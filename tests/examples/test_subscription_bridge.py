from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

from examples.subscription_bridge import server


def test_resolve_backend_prefers_model_prefix() -> None:
    assert server.resolve_backend("codex/gpt-5.4", default_backend="claude") == "codex"
    assert server.resolve_backend("claude/sonnet", default_backend="codex") == "claude"
    assert server.resolve_backend("gpt-5.4", default_backend="codex") == "codex"


def test_resolve_request_model_uses_backend_default_when_model_is_omitted() -> None:
    assert server.resolve_request_model({}, default_backend="codex") == "codex/gpt-5.4"
    assert server.resolve_request_model({}, default_backend="claude") == "claude/claude-sonnet-4-6"
    assert (
        server.resolve_request_model({"model": "claude/claude-sonnet-4-6"}, default_backend="codex")
        == "claude/claude-sonnet-4-6"
    )


def test_http_server_uses_backend_default_model_when_request_omits_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run_backend(*, backend: str, prompt: str, model: str | None, workdir: Path) -> str:
        return f"backend={backend};model={model};workdir={workdir.name}"

    monkeypatch.setattr(server, "run_backend", fake_run_backend)

    httpd = server.make_server("127.0.0.1", 0, default_backend="claude", workdir=tmp_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "Say hi."}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert payload["model"] == "claude/claude-sonnet-4-6"
    assert payload["choices"][0]["message"]["content"].startswith(
        "backend=claude;model=claude/claude-sonnet-4-6"
    )


def test_http_server_returns_502_for_backend_value_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run_backend_structured(
        *, backend: str, prompt: str, model: str | None, workdir: Path, schema: dict[str, Any]
    ) -> dict[str, Any]:
        raise ValueError("backend emitted malformed JSON")

    monkeypatch.setattr(server, "run_backend_structured", fake_run_backend_structured)

    httpd = server.make_server("127.0.0.1", 0, default_backend="codex", workdir=tmp_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "Use the weather tool."}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get the weather for a city.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                },
                            },
                        }
                    ],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert exc_info.value.code == 502
    payload = json.loads(exc_info.value.read().decode("utf-8"))
    assert payload == {"error": {"message": "backend emitted malformed JSON"}}


def test_build_chat_prompt_from_messages_preserves_roles() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Say hi."},
            {"role": "assistant", "content": "Previous reply."},
        ]
    }

    prompt = server.build_chat_prompt(payload)

    assert "[system]\nBe terse." in prompt
    assert "[user]\nSay hi." in prompt
    assert "[assistant]\nPrevious reply." in prompt


def test_build_chat_prompt_includes_tools_and_tool_results() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "Use tools when needed."},
            {"role": "user", "content": "What is the weather in Tokyo?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_weather",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_weather", "content": "Sunny and 72 F"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
    }

    prompt = server.build_chat_prompt(payload)

    assert "Available function tools" in prompt
    assert '"name": "get_weather"' in prompt
    assert '[assistant_tool_call call_weather]\nget_weather {"city":"Tokyo"}' in prompt
    assert "[tool call_weather]\nSunny and 72 F" in prompt
    assert "[tool]\nSunny and 72 F" not in prompt


def test_build_responses_prompt_handles_list_items_and_tool_outputs() -> None:
    payload = {
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "Be terse."}]},
            {"role": "user", "content": [{"type": "input_text", "text": "Say hi."}]},
            {
                "type": "function_call",
                "call_id": "call_weather",
                "name": "get_weather",
                "arguments": '{"city":"Tokyo"}',
            },
            {"type": "function_call_output", "call_id": "call_weather", "output": "Sunny and 72 F"},
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get the weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    }

    prompt = server.build_responses_prompt(payload)

    assert "[system]\nBe terse." in prompt
    assert "[user]\nSay hi." in prompt
    assert '[assistant_tool_call call_weather]\nget_weather {"city":"Tokyo"}' in prompt
    assert "[tool call_weather]\nSunny and 72 F" in prompt


def test_build_chat_completion_response_matches_openai_shape() -> None:
    response = server.build_chat_completion_response(
        model="codex/gpt-5.4",
        request_id="req_123",
        content="hello",
    )

    assert response["object"] == "chat.completion"
    assert response["model"] == "codex/gpt-5.4"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["choices"][0]["message"]["content"] == "hello"
    assert response["usage"]["total_tokens"] == 0


def test_build_chat_completion_response_can_emit_tool_calls() -> None:
    response = server.build_chat_completion_response(
        model="codex/gpt-5.4",
        request_id="req_456",
        tool_calls=[{"name": "get_weather", "arguments": {"city": "Tokyo"}}],
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tool_call = choice["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "get_weather"
    assert json.loads(tool_call["function"]["arguments"]) == {"city": "Tokyo"}


def test_build_responses_api_response_can_emit_function_calls() -> None:
    response = server.build_responses_api_response(
        model="codex/gpt-5.4",
        request_id="req_789",
        tool_calls=[{"name": "get_weather", "arguments": {"city": "Tokyo"}}],
    )

    function_call = response["output"][0]
    assert function_call["type"] == "function_call"
    assert function_call["name"] == "get_weather"
    assert json.loads(function_call["arguments"]) == {"city": "Tokyo"}
    assert response["output_text"] == ""


def test_run_backend_invokes_codex_with_repo_check_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, workdir: Path) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output_file = Path(cmd[cmd.index("-o") + 1])
        output_file.write_text("CODEX_OK\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="ignored", stderr="")

    monkeypatch.setattr(server, "_run_command", fake_run)

    output = server.run_backend(
        backend="codex",
        prompt="Reply with exactly CODEX_OK.",
        model="codex/gpt-5.4",
        workdir=tmp_path,
    )

    assert output == "CODEX_OK"
    assert calls[0][:9] == [
        "codex",
        "exec",
        "-m",
        "gpt-5.4",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(tmp_path),
        "-o",
    ]
    assert calls[0][-1] == "Reply with exactly CODEX_OK."


def test_run_backend_invokes_claude_print_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, workdir: Path) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="CLAUDE_OK\n", stderr="")

    monkeypatch.setattr(server, "_run_command", fake_run)

    output = server.run_backend(
        backend="claude",
        prompt="Reply with exactly CLAUDE_OK.",
        model="claude/claude-sonnet-4-6",
        workdir=tmp_path,
    )

    assert output == "CLAUDE_OK"
    assert calls[0] == [
        "claude",
        "-p",
        "Reply with exactly CLAUDE_OK.",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--model",
        "claude-sonnet-4-6",
    ]


def test_decision_schema_avoids_top_level_one_of_for_codex_compatibility() -> None:
    assert server.DecisionSchema["type"] == "object"
    assert "oneOf" not in server.DecisionSchema
    properties = cast(dict[str, Any], server.DecisionSchema["properties"])
    tool_calls_schema = cast(dict[str, Any], properties["tool_calls"])
    tool_call_item = cast(dict[str, Any], tool_calls_schema["items"])
    assert tool_call_item["properties"]["arguments_json"]["type"] == "string"
    assert server.DecisionSchema["required"] == ["type", "content", "tool_calls"]


def test_structured_decision_prompt_requires_plain_text_final_content() -> None:
    prompt = server._build_structured_decision_prompt(
        "Conversation transcript:\n\n[user]\nSay hi.",
        {"tool_choice": "auto", "parallel_tool_calls": False},
    )

    assert "content must be plain natural-language text only" in prompt
    assert "Do not wrap the final answer in JSON" in prompt


def test_normalize_decision_payload_unwraps_nested_final_json_content() -> None:
    payload = {
        "type": "final",
        "content": '{"content":"The weather in Tokyo is sunny and 72 F.","tool_calls":[]}',
        "tool_calls": [],
    }

    assert server._normalize_decision_payload(payload) == {
        "type": "final",
        "content": "The weather in Tokyo is sunny and 72 F.",
    }


def test_normalize_decision_payload_preserves_nested_tool_calls() -> None:
    payload = {
        "type": "final",
        "content": (
            '{"content":"","tool_calls":[{"name":"get_weather",'
            '"arguments_json":"{\\"city\\":\\"Tokyo\\"}"}]}'
        ),
        "tool_calls": [],
    }

    result = server._normalize_decision_payload(payload)

    assert result["type"] == "tool_calls"
    tool_call = result["tool_calls"][0]
    assert tool_call["name"] == "get_weather"
    assert tool_call["arguments"] == {"city": "Tokyo"}
    assert tool_call["call_id"].startswith("call_")


def test_run_backend_structured_uses_cli_schema_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = (
            '{"type":"tool_calls","tool_calls":[{"name":"get_weather",'
            '"arguments":{"city":"Tokyo"}}]}'
        )
        stderr = ""

    def fake_run(cmd: list[str], *, workdir: Path) -> Result:
        calls.append(cmd)
        if cmd[0] == "codex":
            schema_path = Path(cmd[cmd.index("--output-schema") + 1])
            output_path = Path(cmd[cmd.index("-o") + 1])
            assert json.loads(schema_path.read_text())["type"] == "object"
            output_path.write_text(Result.stdout)
        return Result()

    monkeypatch.setattr(server, "_run_command", fake_run)

    schema = {
        "type": "object",
        "properties": {"type": {"type": "string"}},
        "required": ["type"],
        "additionalProperties": True,
    }
    payload = server.run_backend_structured(
        backend="codex",
        prompt="Return a tool call.",
        model="codex/gpt-5.4",
        workdir=tmp_path,
        schema=schema,
    )

    assert payload["type"] == "tool_calls"
    assert calls[0][0:2] == ["codex", "exec"]


def test_load_json_output_prefers_claude_structured_output_wrapper() -> None:
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": '```json\n{"content":"ignored","tool_calls":[]}\n```',
            "structured_output": {
                "type": "final",
                "content": "The weather in Tokyo is sunny and 72 F.",
                "tool_calls": [],
            },
        }
    )

    assert server._load_json_output(raw) == {
        "type": "final",
        "content": "The weather in Tokyo is sunny and 72 F.",
        "tool_calls": [],
    }


def test_run_backend_structured_uses_claude_json_wrapper_and_two_turn_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "structured_output": {
                    "type": "final",
                    "content": "The weather in Tokyo is sunny and 72 F.",
                    "tool_calls": [],
                },
            }
        )
        stderr = ""

    def fake_run(cmd: list[str], *, workdir: Path) -> Result:
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(server, "_run_command", fake_run)

    schema = {
        "type": "object",
        "properties": {"type": {"type": "string"}},
        "required": ["type"],
        "additionalProperties": True,
    }
    payload = server.run_backend_structured(
        backend="claude",
        prompt="Return a final answer.",
        model="claude/claude-sonnet-4-6",
        workdir=tmp_path,
        schema=schema,
    )

    assert payload == {"type": "final", "content": "The weather in Tokyo is sunny and 72 F."}
    assert calls[0] == [
        "claude",
        "-p",
        "Return a final answer.",
        "--max-turns",
        "2",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--model",
        "claude-sonnet-4-6",
    ]
