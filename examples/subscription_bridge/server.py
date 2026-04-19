from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DecisionSchema = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["final", "tool_calls"]},
        "content": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["name", "arguments_json"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "content", "tool_calls"],
    "additionalProperties": False,
}


def resolve_backend(model: str | None, default_backend: str = "codex") -> str:
    normalized = (model or "").strip().lower()
    if normalized.startswith("codex/"):
        return "codex"
    if normalized.startswith("claude/") or normalized.startswith("anthropic/"):
        return "claude"
    return default_backend


def default_model_for_backend(backend: str) -> str:
    if backend == "claude":
        return "claude/claude-sonnet-4-6"
    return "codex/gpt-5.4"


def resolve_request_model(payload: dict[str, Any], *, default_backend: str) -> str:
    raw_model = payload.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        return raw_model.strip()
    return default_model_for_backend(default_backend)


def _model_flag_value(model: str | None) -> str | None:
    if not model:
        return None
    return model.split("/", 1)[1] if "/" in model else model


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                for key in ("text", "output_text", "input_text"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                        break
                else:
                    if item.get("type") == "input_text":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "output_text", "input_text"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        raw_function = tool.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else tool
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        normalized.append(
            {
                "name": name,
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return normalized


def _format_tool_block(payload: dict[str, Any]) -> str:
    tools = _normalize_tools(payload.get("tools"))
    if not tools:
        return ""
    rendered = json.dumps(tools, indent=2, ensure_ascii=False, sort_keys=True)
    return f"Available function tools:\n{rendered}"


def _describe_tool_choice(tool_choice: Any) -> str:
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            function = tool_choice.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                return f"required:{function['name']}"
        if isinstance(tool_choice.get("name"), str):
            return f"required:{tool_choice['name']}"
    return "auto"


def _required_tool_choice_name(tool_choice: Any) -> str | None:
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") == "function":
        function = tool_choice.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    name = tool_choice.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _tool_choice_requires_tool_calls(tool_choice: Any) -> bool:
    return tool_choice == "required" or _required_tool_choice_name(tool_choice) is not None


def _tool_choice_allows_structured_tool_calls(tool_choice: Any) -> bool:
    return tool_choice != "none"


def _validate_tool_choice_decision(decision: dict[str, Any], payload: dict[str, Any]) -> None:
    tool_choice = payload.get("tool_choice")
    required_tool_name = _required_tool_choice_name(tool_choice)

    if tool_choice == "none":
        if decision.get("type") == "tool_calls":
            raise RuntimeError("tool_choice='none' forbids tool calls")
        return

    if required_tool_name is not None:
        if decision.get("type") != "tool_calls":
            raise RuntimeError(
                f"required tool choice {required_tool_name!r} requires a tool call"
            )
        invalid_names = [
            tool_call.get("name")
            for tool_call in decision.get("tool_calls", [])
            if tool_call.get("name") != required_tool_name
        ]
        if invalid_names:
            raise RuntimeError(
                f"backend violated required tool choice {required_tool_name!r}"
            )
        return

    if _tool_choice_requires_tool_calls(tool_choice) and decision.get("type") != "tool_calls":
        raise RuntimeError("tool_choice='required' requires a tool call")


def _chat_message_blocks(messages: Any) -> list[str]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("chat.completions payload must include non-empty messages")

    blocks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = _flatten_content(message.get("content"))
        if content and role != "tool":
            blocks.append(f"[{role}]\n{content}")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    raw_function = tool_call.get("function")
                    function: dict[str, Any] = (
                        raw_function if isinstance(raw_function, dict) else {}
                    )
                    name = function.get("name")
                    arguments = function.get("arguments")
                    call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex}"
                    if isinstance(name, str):
                        arg_text = (
                            arguments if isinstance(arguments, str) else json.dumps(arguments or {})
                        )
                        blocks.append(f"[assistant_tool_call {call_id}]\n{name} {arg_text}")
        if role == "tool":
            tool_call_id = message.get("tool_call_id") or f"call_{uuid.uuid4().hex}"
            tool_text = _flatten_content(message.get("content"))
            if tool_text:
                blocks.append(f"[tool {tool_call_id}]\n{tool_text}")
    return blocks


def _responses_input_blocks(items: Any) -> list[str]:
    if isinstance(items, str) and items.strip():
        return [f"[user]\n{items.strip()}"]
    if not isinstance(items, list) or not items:
        raise ValueError("responses payload must include non-empty input")

    blocks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or f"call_{uuid.uuid4().hex}"
            name = item.get("name")
            arguments = item.get("arguments")
            if isinstance(name, str):
                arg_text = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
                blocks.append(f"[assistant_tool_call {call_id}]\n{name} {arg_text}")
            continue
        if item_type == "function_call_output":
            call_id = item.get("call_id") or f"call_{uuid.uuid4().hex}"
            output = _flatten_content(item.get("output"))
            if output:
                blocks.append(f"[tool {call_id}]\n{output}")
            continue
        role = str(item.get("role") or "user")
        content = _flatten_content(item.get("content"))
        if content:
            blocks.append(f"[{role}]\n{content}")
    return blocks


def build_chat_prompt(payload: dict[str, Any]) -> str:
    blocks = _chat_message_blocks(payload.get("messages"))
    if not blocks:
        raise ValueError("chat.completions payload must include at least one text or tool message")

    parts = [
        "You are servicing an OpenAI-compatible chat.completions request via a CLI model.",
        _format_tool_block(payload),
        f"Tool choice constraint: {_describe_tool_choice(payload.get('tool_choice'))}",
        "Conversation transcript:",
        "\n\n".join(blocks),
        "If no structured schema is requested, return only the assistant's next natural-language message.",
    ]
    return "\n\n".join(part for part in parts if part)


def build_responses_prompt(payload: dict[str, Any]) -> str:
    blocks = _responses_input_blocks(payload.get("input"))
    if not blocks:
        raise ValueError("responses payload must include at least one text or tool item")

    parts = [
        "You are servicing an OpenAI-compatible responses.create request via a CLI model.",
        _format_tool_block(payload),
        f"Tool choice constraint: {_describe_tool_choice(payload.get('tool_choice'))}",
        "Conversation transcript:",
        "\n\n".join(blocks),
        "If no structured schema is requested, return only the assistant's next natural-language message.",
    ]
    return "\n\n".join(part for part in parts if part)


def _build_structured_decision_prompt(base_prompt: str, payload: dict[str, Any]) -> str:
    raw_tool_choice = payload.get("tool_choice")
    tool_choice = _describe_tool_choice(raw_tool_choice)
    required_tool_name = _required_tool_choice_name(raw_tool_choice)
    parallel_tool_calls = bool(payload.get("parallel_tool_calls"))
    instructions = [
        "Return JSON only.",
        "Choose exactly one action for the next assistant turn.",
        "If enough information is already available, return a final answer.",
        "If tool use is required, return tool_calls using only the listed function tools.",
        f"Tool choice constraint: {tool_choice}.",
        f"Parallel tool calls allowed: {'yes' if parallel_tool_calls else 'no'}.",
        "Always include both content and tool_calls in the JSON.",
        "For a final answer, set tool_calls to [] and put the answer in content.",
        "For a final answer, content must be plain natural-language text only.",
        "Do not wrap the final answer in JSON, quoted JSON, markdown fences, or any other envelope.",
        "For tool use, set content to an empty string and put one or more tool call objects in tool_calls.",
        "When you emit tool_calls, arguments_json must be a valid JSON string encoding an object that matches the tool schema.",
        "Do not invent tools.",
    ]
    if raw_tool_choice == "required":
        instructions.append("You must return at least one tool call.")
    if required_tool_name is not None:
        instructions.append("You must return at least one tool call.")
        instructions.append(f"Every tool call name must be exactly {required_tool_name}.")
    return f"{base_prompt}\n\nDecision rules:\n- " + "\n- ".join(instructions)


def _build_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _coerce_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        name = tool_call.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = tool_call.get("arguments")
        arguments_json = tool_call.get("arguments_json")
        if isinstance(arguments_json, str):
            try:
                arguments = json.loads(arguments_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"tool call arguments_json was not valid JSON: {arguments_json}"
                ) from exc
        elif isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"value": arguments}
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            raise ValueError("tool call arguments must decode to a JSON object")
        normalized.append(
            {
                "call_id": tool_call.get("call_id") or f"call_{uuid.uuid4().hex}",
                "name": name,
                "arguments": arguments,
            }
        )
    if not normalized:
        raise ValueError("tool_calls payload must include at least one valid tool call")
    return normalized


def build_chat_completion_response(
    model: str,
    request_id: str,
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    created = int(time.time())
    if tool_calls:
        normalized_tool_calls = _coerce_tool_calls(tool_calls)
        return {
            "id": request_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call["call_id"],
                                "type": "function",
                                "function": {
                                    "name": tool_call["name"],
                                    "arguments": json.dumps(
                                        tool_call["arguments"], separators=(",", ":")
                                    ),
                                },
                            }
                            for tool_call in normalized_tool_calls
                        ],
                    },
                }
            ],
            "usage": _build_usage(),
        }

    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content or ""},
            }
        ],
        "usage": _build_usage(),
    }


def build_responses_api_response(
    model: str,
    request_id: str,
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    created = int(time.time())
    if tool_calls:
        normalized_tool_calls = _coerce_tool_calls(tool_calls)
        return {
            "id": request_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": model,
            "output": [
                {
                    "id": f"fc_{uuid.uuid4().hex}",
                    "type": "function_call",
                    "call_id": tool_call["call_id"],
                    "name": tool_call["name"],
                    "arguments": json.dumps(tool_call["arguments"], separators=(",", ":")),
                    "status": "completed",
                }
                for tool_call in normalized_tool_calls
            ],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "output_text": "",
        }

    text = content or ""
    return {
        "id": request_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "output_text": text,
    }


def _run_command(cmd: list[str], *, workdir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        check=False,
    )


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return text


def _load_json_output(raw: str) -> dict[str, Any]:
    candidate = _strip_code_fence(raw)
    payload = json.loads(candidate)
    if isinstance(payload, dict):
        structured_output = payload.get("structured_output")
        if isinstance(structured_output, dict) and isinstance(structured_output.get("type"), str):
            return structured_output
        for key in ("result", "output", "response"):
            nested = payload.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("type"), str):
                return nested
            if isinstance(nested, str):
                nested_candidate = _strip_code_fence(nested)
                try:
                    nested_payload = json.loads(nested_candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(nested_payload, dict):
                    return nested_payload
        return payload
    raise RuntimeError("backend returned non-object JSON payload")


def _extract_nested_decision_payload(content: str) -> dict[str, Any] | None:
    current = content
    for _ in range(3):
        candidate = _strip_code_fence(current).strip()
        if not candidate:
            return {"type": "final", "content": ""}
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None

        decision_type = parsed.get("type")
        content_value = parsed.get("content")
        tool_calls = parsed.get("tool_calls")
        if decision_type == "tool_calls" or (isinstance(tool_calls, list) and tool_calls):
            return {"type": "tool_calls", "tool_calls": tool_calls}

        if decision_type == "final" or (
            set(parsed).issubset({"type", "content", "tool_calls"})
            and isinstance(content_value, str)
        ):
            if not isinstance(content_value, str):
                return None
            nested_candidate = _strip_code_fence(content_value).strip()
            if not nested_candidate:
                return {"type": "final", "content": ""}
            try:
                nested = json.loads(nested_candidate)
            except json.JSONDecodeError:
                return {"type": "final", "content": content_value}
            if isinstance(nested, dict) and set(nested).intersection(
                {"type", "content", "tool_calls"}
            ):
                current = content_value
                continue
            return {"type": "final", "content": content_value}

        return None
    return None


def _normalize_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    decision_type = payload.get("type")
    if decision_type == "final":
        content = payload.get("content")
        if not isinstance(content, str):
            raise RuntimeError("structured backend response missing final content")
        nested_payload = _extract_nested_decision_payload(content)
        if nested_payload:
            return _normalize_decision_payload(nested_payload)
        return {"type": "final", "content": content}
    if decision_type == "tool_calls":
        tool_calls = payload.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            raise RuntimeError("structured backend response missing tool_calls")
        return {"type": "tool_calls", "tool_calls": _coerce_tool_calls(tool_calls)}
    raise RuntimeError(f"structured backend response has unsupported type: {decision_type!r}")


def run_backend(backend: str, prompt: str, model: str | None, workdir: Path) -> str:
    model_name = _model_flag_value(model)
    if backend == "codex":
        with tempfile.TemporaryDirectory(prefix="subscription-bridge-") as tmp_dir:
            output_path = Path(tmp_dir) / "codex-last-message.txt"
            cmd = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "-C",
                str(workdir),
                "-o",
                str(output_path),
                prompt,
            ]
            if model_name:
                cmd[2:2] = ["-m", model_name]
            result = _run_command(cmd, workdir=workdir)
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "backend execution failed").strip()
                raise RuntimeError(stderr)
            text = (
                output_path.read_text().strip() if output_path.exists() else result.stdout.strip()
            )
            if not text:
                raise RuntimeError("backend returned empty output")
            return text

    if backend == "claude":
        cmd = ["claude", "-p", prompt, "--max-turns", "1", "--no-session-persistence"]
        if model_name:
            cmd.extend(["--model", model_name])
        result = _run_command(cmd, workdir=workdir)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "backend execution failed").strip()
            raise RuntimeError(stderr)
        text = result.stdout.strip()
        if not text:
            raise RuntimeError("backend returned empty output")
        return text

    raise ValueError(f"Unsupported backend: {backend}")


def run_backend_structured(
    backend: str,
    prompt: str,
    model: str | None,
    workdir: Path,
    schema: dict[str, Any],
) -> dict[str, Any]:
    model_name = _model_flag_value(model)
    if backend == "codex":
        with tempfile.TemporaryDirectory(prefix="subscription-bridge-") as tmp_dir:
            temp_dir = Path(tmp_dir)
            schema_path = temp_dir / "schema.json"
            output_path = temp_dir / "codex-structured.json"
            schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False))
            cmd = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "-C",
                str(workdir),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                prompt,
            ]
            if model_name:
                cmd[2:2] = ["-m", model_name]
            result = _run_command(cmd, workdir=workdir)
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "backend execution failed").strip()
                raise RuntimeError(stderr)
            raw = output_path.read_text() if output_path.exists() else result.stdout
            try:
                return _normalize_decision_payload(_load_json_output(raw))
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc

    if backend == "claude":
        cmd = [
            "claude",
            "-p",
            prompt,
            "--max-turns",
            "2",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if model_name:
            cmd.extend(["--model", model_name])
        result = _run_command(cmd, workdir=workdir)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "backend execution failed").strip()
            raise RuntimeError(stderr)
        try:
            return _normalize_decision_payload(_load_json_output(result.stdout))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    raise ValueError(f"Unsupported backend: {backend}")


def _respond_for_chat_request(
    payload: dict[str, Any], *, backend: str, model: str, workdir: Path, request_id: str
) -> dict[str, Any]:
    prompt = build_chat_prompt(payload)
    if _normalize_tools(payload.get("tools")) and _tool_choice_allows_structured_tool_calls(
        payload.get("tool_choice")
    ):
        try:
            decision = run_backend_structured(
                backend=backend,
                prompt=_build_structured_decision_prompt(prompt, payload),
                model=model,
                workdir=workdir,
                schema=DecisionSchema,
            )
            _validate_tool_choice_decision(decision, payload)
            if decision.get("type") == "tool_calls":
                return build_chat_completion_response(
                    model=model,
                    request_id=request_id,
                    tool_calls=decision.get("tool_calls"),
                )
            return build_chat_completion_response(
                model=model,
                request_id=request_id,
                content=str(decision.get("content") or ""),
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    text = run_backend(backend=backend, prompt=prompt, model=model, workdir=workdir)
    return build_chat_completion_response(model=model, request_id=request_id, content=text)


def _respond_for_responses_request(
    payload: dict[str, Any], *, backend: str, model: str, workdir: Path, request_id: str
) -> dict[str, Any]:
    prompt = build_responses_prompt(payload)
    if _normalize_tools(payload.get("tools")) and _tool_choice_allows_structured_tool_calls(
        payload.get("tool_choice")
    ):
        try:
            decision = run_backend_structured(
                backend=backend,
                prompt=_build_structured_decision_prompt(prompt, payload),
                model=model,
                workdir=workdir,
                schema=DecisionSchema,
            )
            _validate_tool_choice_decision(decision, payload)
            if decision.get("type") == "tool_calls":
                return build_responses_api_response(
                    model=model,
                    request_id=request_id,
                    tool_calls=decision.get("tool_calls"),
                )
            return build_responses_api_response(
                model=model,
                request_id=request_id,
                content=str(decision.get("content") or ""),
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    text = run_backend(backend=backend, prompt=prompt, model=model, workdir=workdir)
    return build_responses_api_response(model=model, request_id=request_id, content=text)


class SubscriptionBridgeHandler(BaseHTTPRequestHandler):
    server_version = "SubscriptionBridge/0.2"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "backend": self.server.default_backend})  # type: ignore[attr-defined]
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Not found"}})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            default_backend = self.server.default_backend  # type: ignore[attr-defined]
            model = resolve_request_model(payload, default_backend=default_backend)
            backend = resolve_backend(model, default_backend=default_backend)
            workdir = Path(self.server.workdir)  # type: ignore[attr-defined]
            request_id = f"bridge_{uuid.uuid4().hex}"

            if self.path == "/v1/chat/completions":
                self._send_json(
                    HTTPStatus.OK,
                    _respond_for_chat_request(
                        payload,
                        backend=backend,
                        model=model,
                        workdir=workdir,
                        request_id=request_id,
                    ),
                )
                return

            if self.path == "/v1/responses":
                self._send_json(
                    HTTPStatus.OK,
                    _respond_for_responses_request(
                        payload,
                        backend=backend,
                        model=model,
                        workdir=workdir,
                        request_id=request_id,
                    ),
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Not found"}})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
        except RuntimeError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": {"message": str(exc)}})
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": str(exc)}})


def make_server(
    host: str, port: int, *, default_backend: str, workdir: Path
) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), SubscriptionBridgeHandler)
    httpd.default_backend = default_backend  # type: ignore[attr-defined]
    httpd.workdir = str(workdir)  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible bridge backed by Codex or Claude CLI plans"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--backend", choices=["codex", "claude"], default="codex")
    parser.add_argument("--workdir", default=str(Path.cwd()))
    args = parser.parse_args()

    httpd = make_server(
        args.host,
        args.port,
        default_backend=args.backend,
        workdir=Path(args.workdir).resolve(),
    )
    print(f"subscription bridge listening on http://{args.host}:{args.port} via {args.backend}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
