from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from openai.types.responses import ResponseFunctionToolCall
from pydantic import TypeAdapter

from .._tool_identity import (
    get_explicit_function_tool_namespace,
    get_function_tool_namespace_description,
)
from ..exceptions import UserError
from ..handoffs import Handoff
from ..items import ItemHelpers, TResponseInputItem, TResponseOutputItem
from ..tool import ApplyPatchTool, FunctionTool, ShellTool, Tool, ToolSearchTool, WebSearchTool


def _image_content(part: dict[str, Any]) -> dict[str, Any]:
    image = part.get("image_url")
    if isinstance(image, str) and not image.startswith("data:"):
        parsed = urlparse(image)
        if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
            image = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[a-zA-Z]:[/\\]", image):
                image = image[1:]
        path = Path(image)
        if not path.is_absolute() or str(path).replace("\\", "/").startswith("//"):
            raise UserError("CodexProvider accepts images as local paths or image data URLs.")
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise UserError("CodexProvider accepts PNG, JPEG, WebP, and GIF images.")
        try:
            image = f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode()}"
        except (OSError, ValueError) as exc:
            raise UserError("CodexProvider cannot read the local image.") from exc
    if not isinstance(image, str) or not re.match(
        r"^data:image/(?:png|jpeg|webp|gif);base64,", image, re.IGNORECASE
    ):
        raise UserError("CodexProvider accepts images as local paths or image data URLs.")
    result: dict[str, Any] = {"type": "input_image", "image_url": image}
    if part.get("detail") is not None:
        result["detail"] = part["detail"]
    return result


def _audio_content(part: dict[str, Any]) -> dict[str, Any]:
    audio = part.get("input_audio")
    if not isinstance(audio, dict) or audio.get("format") not in ("wav", "mp3"):
        raise UserError("CodexProvider accepts inline WAV and MP3 audio.")
    data = audio.get("data")
    try:
        decoded = base64.b64decode(data, validate=True) if isinstance(data, str) else b""
    except ValueError:
        raise UserError("CodexProvider requires base64 audio data.") from None
    if audio["format"] == "wav":
        valid = decoded[:4] == b"RIFF" and decoded[8:12] == b"WAVE"
    else:
        valid = decoded[:3] == b"ID3" or (
            len(decoded) > 1 and decoded[0] == 255 and decoded[1] & 224 == 224
        )
    if not valid:
        raise UserError(
            "CodexProvider requires WAV or MP3 audio bytes matching the declared format."
        )
    media_type = "audio/wav" if audio["format"] == "wav" else "audio/mpeg"
    return {"type": "input_audio", "audio_url": f"data:{media_type};base64,{data}"}


def _user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    result = []
    for part in content:
        if part.get("prompt_cache_breakpoint") is not None:
            raise UserError("CodexProvider does not support prompt cache breakpoints.")
        if part["type"] == "input_text":
            result.append({"type": "input_text", "text": part["text"]})
        elif part["type"] == "input_image":
            result.append(_image_content(part))
        elif part["type"] == "input_audio":
            result.append(_audio_content(part))
        else:
            raise UserError(
                "CodexProvider accepts only text, local images, and inline audio in user content."
            )
    return result


def prepare_codex_history(
    input: str | list[TResponseInputItem],
    tools: list[Tool] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate SDK replay items and separate the trailing user turn."""
    history: list[dict[str, Any]] = []
    pending: dict[str, str] = {}
    for item in cast(list[dict[str, Any]], ItemHelpers.input_to_new_input_list(input)):
        kind = item.get("type", "message")
        if kind in ("shell_call", "apply_patch_call"):
            tool_class = ShellTool if kind == "shell_call" else ApplyPatchTool
            name = item.get("provider_data", {}).get("codex_tool_name") or next(
                (tool.name for tool in tools or [] if isinstance(tool, tool_class)),
                "shell" if kind == "shell_call" else "apply_patch",
            )
            item = {
                "type": "function_call",
                "namespace": "agents_sdk",
                "name": name,
                "call_id": item["call_id"],
                "arguments": json.dumps(
                    {
                        "action" if kind == "shell_call" else "operation": item[
                            "action" if kind == "shell_call" else "operation"
                        ]
                    }
                ),
            }
            kind = "function_call"
        elif kind in ("shell_call_output", "apply_patch_call_output"):
            item = {
                "type": "function_call_output",
                "call_id": item["call_id"],
                "output": json.dumps(
                    {
                        key: value
                        for key, value in item.items()
                        if key not in ("id", "type", "call_id")
                    }
                ),
            }
            kind = "function_call_output"
        if kind in ("tool_search_call", "tool_search_output"):
            keys = (
                ("type", "id", "call_id", "status", "execution", "arguments")
                if kind == "tool_search_call"
                else ("type", "id", "call_id", "status", "execution", "tools")
            )
            history.append({key: item[key] for key in keys if key in item})
        elif kind == "web_search_call":
            history.append(
                {
                    key: item[key]
                    for key in ("type", "id", "status", "action", "encrypted_content")
                    if key in item
                }
            )
        elif kind == "reasoning":
            history.append(
                {
                    key: item[key]
                    for key in ("type", "id", "summary", "content", "encrypted_content")
                    if key in item
                }
            )
        elif kind == "compaction":
            history.append(
                {key: item[key] for key in ("type", "id", "encrypted_content") if key in item}
            )
        elif kind in ("function_call", "custom_tool_call"):
            call_id = item["call_id"]
            if call_id in pending:
                raise UserError("CodexProvider received a duplicate tool call.")
            pending[call_id] = kind
            history.append(
                {
                    key: item[key]
                    for key in ("type", "name", "namespace", "call_id", "arguments", "input")
                    if key in item
                }
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = item["call_id"]
            if pending.get(call_id, "") + "_output" != kind:
                raise UserError("CodexProvider requires each tool output to match a tool call.")
            pending.pop(call_id)
            output = item["output"]
            history.append(
                {
                    "type": kind,
                    "call_id": call_id,
                    "output": output if isinstance(output, str) else _user_content(output),
                }
            )
        elif kind == "message" and item.get("role") in ("user", "assistant", "developer"):
            role = item["role"]
            if role in ("user", "developer"):
                content = _user_content(item["content"])
            elif isinstance(item["content"], str):
                content = [{"type": "output_text", "text": item["content"]}]
            else:
                content = []
                for part in item["content"]:
                    if part.get("prompt_cache_breakpoint") is not None:
                        raise UserError("CodexProvider does not support prompt cache breakpoints.")
                    if part["type"] != "output_text":
                        raise UserError("CodexProvider accepts only text in assistant history.")
                    content.append({"type": "output_text", "text": part["text"]})
            message: dict[str, Any] = {"type": "message", "role": role, "content": content}
            if role == "assistant" and item.get("phase") is not None:
                message["phase"] = item["phase"]
            history.append(message)
        else:
            raise UserError("CodexProvider received an unsupported history item.")
    if pending:
        raise UserError("CodexProvider cannot replay a pending tool call.")
    if not history:
        raise UserError("CodexProvider requires conversation input.")
    tail = len(history)
    while tail and history[tail - 1].get("role") == "user":
        tail -= 1
    turn_input = []
    for message in history[tail:]:
        for part in message["content"]:
            if part["type"] == "input_text":
                turn_input.append({"type": "text", "text": part["text"], "text_elements": []})
            elif part["type"] == "input_audio":
                turn_input.append({"type": "audio", "audio_url": part["audio_url"]})
            else:
                turn_input.append(
                    {
                        "type": "image",
                        "image_url": part["image_url"],
                        **({"detail": part["detail"]} if "detail" in part else {}),
                    }
                )
    if tail < len(history) and not any(
        part["type"] in ("image", "audio") or part["text"].strip() for part in turn_input
    ):
        raise UserError("CodexProvider requires user text or a local image.")
    return history[:tail], turn_input


def prepare_dynamic_tools(
    tools: list[Tool], handoffs: list[Handoff]
) -> tuple[list[dict[str, Any]], set[tuple[str | None, str]]]:
    """Declare SDK tools without invoking them or taking approval ownership."""
    declarations: list[dict[str, Any]] = []
    identities: set[tuple[str | None, str]] = set()
    namespaces: dict[str, dict[str, Any]] = {}
    if any(
        sum(isinstance(tool, kind) for tool in tools) > 1 for kind in (ShellTool, ApplyPatchTool)
    ):
        raise UserError(
            "CodexProvider supports one local shell and one apply-patch callback tool per request."
        )

    def add(
        spec: dict[str, Any], namespace: str | None = None, description: str | None = None
    ) -> None:
        identity = (namespace, spec["name"])
        if identity in identities:
            raise UserError("CodexProvider requires unique SDK tool names.")
        identities.add(identity)
        if namespace is None:
            declarations.append(spec)
            return
        if namespace not in namespaces:
            if not description:
                raise UserError("CodexProvider requires a tool namespace description.")
            group = {
                "type": "namespace",
                "name": namespace,
                "description": description,
                "tools": [],
            }
            namespaces[namespace] = group
            declarations.append(group)
        namespaces[namespace]["tools"].append(spec)

    for tool in tools:
        if isinstance(tool, WebSearchTool):
            continue
        if isinstance(tool, ToolSearchTool):
            if (
                tool.execution not in (None, "server")
                or tool.description is not None
                or tool.parameters is not None
            ):
                raise UserError(
                    "CodexProvider uses stock server tool discovery without custom search settings."
                )
            continue
        if isinstance(tool, (ShellTool, ApplyPatchTool)):
            if any(caller != "direct" for caller in tool.allowed_callers or []):
                raise UserError("CodexProvider supports only direct SDK tool callers.")
            if isinstance(tool, ShellTool):
                if (
                    tool.executor is None
                    or not tool.environment
                    or tool.environment["type"] != "local"
                ):
                    raise UserError("CodexProvider supports ShellTool only with a local executor.")
                schema = {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "object",
                            "properties": {
                                "commands": {"type": "array", "items": {"type": "string"}},
                                "timeout_ms": {"type": "integer"},
                                "max_output_length": {"type": "integer"},
                            },
                            "required": ["commands"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                }
                description = "Execute shell commands using the SDK executor."
                if skills := tool.environment.get("skills"):
                    declared = [
                        {
                            "name": skill["name"],
                            "description": skill["description"],
                            "path": skill["path"],
                        }
                        for skill in skills
                    ]
                    description += (
                        f" Available local skills: {json.dumps(declared)}. "
                        "Read SKILL.md in the selected skill directory using this shell."
                    )
            else:
                operations = []
                for kind in ("create_file", "update_file", "delete_file"):
                    properties = {
                        "type": {"type": "string", "enum": [kind]},
                        "path": {"type": "string"},
                    }
                    if kind != "delete_file":
                        properties["diff"] = {"type": "string"}
                    required = list(properties)
                    if kind == "update_file":
                        properties["move_to"] = {"type": "string"}
                    operations.append(
                        {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        }
                    )
                schema = {
                    "type": "object",
                    "properties": {"operation": {"anyOf": operations}},
                    "required": ["operation"],
                    "additionalProperties": False,
                }
                description = "Apply a file operation using the SDK editor."
            add(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": description,
                    "inputSchema": schema,
                },
                "agents_sdk",
                "SDK callback tools",
            )
            continue
        if not isinstance(tool, FunctionTool) or any(
            caller != "direct" for caller in tool.allowed_callers or []
        ):
            raise UserError("CodexProvider supports direct SDK function tools and handoffs.")
        add(
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.params_json_schema,
                **({"deferLoading": True} if tool.defer_loading else {}),
            },
            get_explicit_function_tool_namespace(tool)
            or (tool.name if tool.defer_loading else None),
            get_function_tool_namespace_description(tool)
            or (tool.description or tool.name if tool.defer_loading else None),
        )
    for handoff in handoffs:
        add(
            {
                "type": "function",
                "name": handoff.tool_name,
                "description": handoff.tool_description,
                "inputSchema": handoff.input_json_schema,
            }
        )
    return declarations, identities


def sdk_tool_call(
    params: dict[str, Any],
    identities: set[tuple[str | None, str]],
    tools: list[Tool] | None = None,
) -> TResponseOutputItem:
    """Return a model call for the SDK runner to execute and approve."""
    call_id, namespace, name = params.get("callId"), params.get("namespace"), params.get("tool")
    if (
        not isinstance(call_id, str)
        or not call_id
        or not isinstance(name, str)
        or not (namespace is None or isinstance(namespace, str))
        or (namespace, name) not in identities
        or "arguments" not in params
    ):
        raise UserError("Codex requested an unrecognized SDK tool call.")
    local_tool = next(
        (
            tool
            for tool in tools or []
            if isinstance(tool, (ShellTool, ApplyPatchTool))
            and tool.name == name
            and namespace == "agents_sdk"
        ),
        None,
    )
    if local_tool is not None:
        kind = "shell_call" if isinstance(local_tool, ShellTool) else "apply_patch_call"
        field = "action" if isinstance(local_tool, ShellTool) else "operation"
        return TypeAdapter(TResponseOutputItem).validate_python(
            {
                "type": kind,
                "id": call_id,
                "call_id": call_id,
                "status": "in_progress",
                "provider_data": {"codex_tool_name": name},
                field: params["arguments"][field],
            }
        )
    return ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=json.dumps(params["arguments"], separators=(",", ":"), ensure_ascii=False),
        status="completed",
        **cast(dict[str, Any], {"namespace": namespace} if namespace is not None else {}),
    )
