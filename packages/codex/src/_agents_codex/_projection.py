"""Project native observations into Managed Agents event envelopes."""

import base64 as base64
import codecs as codecs
import copy as copy
import re as re
import shlex as shlex
import time as time
from typing import Any as Any, Literal as Literal, cast as cast

from ._events import (
    CommandOutputDeltaEvent as CommandOutputDeltaEvent,
    EnvironmentError as EnvironmentError,
    EnvironmentStatus as EnvironmentStatus,
    ErrorEvent as ErrorEvent,
    ExecutionItem as ExecutionItem,
    Failure as Failure,
    NativeEvent as NativeEvent,
    RequiredAction as RequiredAction,
    SessionCreatedEvent as SessionCreatedEvent,
    SessionEvent as SessionEvent,
    SessionSnapshot as SessionSnapshot,
    TokenUsage as TokenUsage,
)
from ._public import preserve_module_identity as _preserve_module_identity
from ._subagent_projection import project_subagent_item as project_subagent_item
from ._web_projection import project_web_search_item as project_web_search_item
from .input import _response_content
from .subagents import Subagent as Subagent


def _usage(native: dict[str, Any]) -> TokenUsage:
    usage: TokenUsage = {
        "input_tokens": native.get("input_tokens", 0),
        "input_tokens_details": {"cached_tokens": native.get("cached_input_tokens", 0)},
        "output_tokens": native.get("output_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": native.get("reasoning_output_tokens", 0)},
        "total_tokens": native.get("total_tokens", 0),
    }
    if "cache_write_input_tokens" in native:
        usage["input_tokens_details"]["cache_write_tokens"] = native["cache_write_input_tokens"]
    return usage


def _error_code(error: dict[str, Any]) -> str | None:
    info = error.get("codex_error_info")
    details: object = {}
    if isinstance(info, dict):
        if len(info) != 1:
            return None
        info, details = next(iter(info.items()))
    if not isinstance(info, str):
        return None
    code = re.sub(r"(?<!^)(?=[A-Z])", "_", info).lower()
    if code in {
        "http_connection_failed",
        "response_stream_connection_failed",
        "response_stream_disconnected",
        "response_too_many_failed_attempts",
    }:
        status = details.get("http_status_code") if isinstance(details, dict) else None
        if isinstance(status, int) and not isinstance(status, bool):
            mapped = {
                401: "authentication_error",
                403: "authentication_error",
                402: "usage_limit_exceeded",
                404: "resource_not_found",
                408: "request_timeout",
                504: "request_timeout",
                429: "rate_limit_exceeded",
                503: "server_overloaded",
                529: "server_overloaded",
            }
            if status in mapped:
                return mapped[status]
            if 400 <= status < 500:
                return "invalid_request"
            if 500 <= status < 600:
                return "server_error"
        return (
            "server_error" if code == "response_too_many_failed_attempts" else "connection_failed"
        )
    aliases = {
        "context_window_exceeded": "context_length_exceeded",
        "unauthorized": "authentication_error",
        "bad_request": "invalid_request",
        "internal_server_error": "server_error",
        "thread_rollback_failed": "server_error",
        "other": "internal_error",
    }
    if code in aliases:
        return aliases[code]
    return (
        code
        if code
        in {
            "session_budget_exceeded",
            "model_invocation_deadline_exceeded",
            "usage_limit_exceeded",
            "project_spend_limit_exceeded",
            "organization_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
            "billing_not_active",
            "credit_balance_exhausted",
            "rate_limit_exceeded",
            "flex_unavailable",
            "server_overloaded",
            "cyber_policy",
            "misalignment_policy_violation",
            "sandbox_error",
            "active_turn_not_steerable",
        }
        else None
    )


def _error_http_status(error: dict[str, Any], fallback: int | None = None) -> int | None:
    """Return an observed overload status, never infer one from its error code."""
    if _error_code(error) != "server_overloaded":
        return None
    info = error.get("codex_error_info")
    if isinstance(info, dict) and len(info) == 1:
        details = next(iter(info.values()))
        if isinstance(details, dict):
            status = details.get("http_status_code")
            if type(status) is int and 400 <= status <= 599:
                return status
    return fallback


_EXECUTION_KINDS = {"CommandExecution", "FileChange", "McpToolCall"}


def _execution_item(
    native: dict[str, Any], thread_id: str | None, turn_id: str
) -> dict[str, Any] | None:
    item_id = native.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    native_status = native.get("status")
    status = {
        "inProgress": "in_progress",
        "declined": "incomplete",
    }.get(native_status or "", native_status or "incomplete")
    common = {
        "id": item_id,
        "call_id": item_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "status": status,
        "native_status": native_status,
    }
    if native["type"] == "McpToolCall":
        return {
            **common,
            "type": "mcp_call",
            "server_label": native["server"],
            "name": native["tool"],
            "arguments": native["arguments"],
            "output": native.get("result"),
            "error": native.get("error"),
        }
    if native["type"] == "CommandExecution":
        duration = native.get("duration")
        duration_ms = None
        if isinstance(duration, dict):
            duration_ms = duration.get("secs", 0) * 1000 + duration.get("nanos", 0) // 1_000_000
        return {
            **common,
            "type": "command_execution",
            "command": shlex.join(native["command"]),
            "argv": native["command"],
            "cwd": native.get("cwd"),
            "output": native.get("aggregated_output"),
            "exit_code": native.get("exit_code"),
            "duration_ms": duration_ms,
        }
    if native["type"] == "FileChange":
        return {
            **common,
            "type": "patch_execution",
            "changes": native["changes"],
            "stdout": native.get("stdout"),
            "stderr": native.get("stderr"),
        }
    return None


def project_execution_completion(
    message: dict[str, Any], *, thread_id: str | None, turn_id: str
) -> ExecutionItem | None:
    """Project a canonical completion owned by this exact native turn."""
    if (
        message["type"] != "item_completed"
        or message.get("thread_id") != thread_id
        or message.get("turn_id") != turn_id
    ):
        return None
    native = message.get("item")
    if (
        not isinstance(native, dict)
        or native.get("type") not in _EXECUTION_KINDS
        or native.get("status") in {"in_progress", "inProgress"}
    ):
        return None
    return cast(ExecutionItem | None, _execution_item(native, thread_id, turn_id))


def project_hosted_response_item(raw: dict[str, Any], turn_id: str | None) -> dict[str, Any] | None:
    """Keep the complete provider payload alongside its public typed fields."""
    raw = {
        key: value
        for key, value in raw.items()
        if key != "internal_chat_message_metadata_passthrough"
    }
    fields = {
        "file_search_call": ("queries", "results"),
        "code_interpreter_call": ("container_id", "code", "outputs"),
        "image_generation_call": ("result", "revised_prompt"),
    }.get(raw.get("type", ""))
    if fields is None or not raw.get("id"):
        return None
    return {
        "type": raw["type"],
        "id": raw["id"],
        "turn_id": turn_id,
        "status": raw["status"],
        "provider_data": raw,
        **{key: raw.get(key) for key in fields},
    }


def project_model_output_item(
    native: dict[str, Any], done: bool, turn_id: str | None
) -> dict[str, Any] | None:
    if native["type"] == "hosted_tool_call":
        return project_hosted_response_item(native["item"], turn_id)
    if native["type"] == "ImageGeneration":
        raw = native.get("provider_data")
        if not isinstance(raw, dict):
            # Historical Core events predate the complete provider payload.
            raw = {
                key: value
                for key, value in native.items()
                if key not in {"saved_path", "provider_data"}
            }
            raw["type"] = "image_generation_call"
        return project_hosted_response_item(raw, turn_id)
    if native["type"] == "Reasoning":
        return {
            "id": native["id"],
            "turn_id": turn_id,
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": text} for text in native.get("summary_text", [])
            ],
            "status": "completed" if done else "in_progress",
        }
    if native["type"] != "AgentMessage":
        return project_web_search_item(native, done, turn_id)
    content = []
    for part in native.get("content", []):
        if part.get("type") == "Text":
            text = {"type": "output_text", "text": part["text"]}
            if part.get("annotations") is not None:
                text["annotations"] = part["annotations"]
            content.append(text)
    item = {
        "id": native["id"],
        "turn_id": turn_id,
        "type": "message",
        "role": "assistant",
        "status": "completed" if done else "in_progress",
        "content": content,
        "phase": native.get("phase"),
    }
    if native.get("display_text") is not None:
        item["display_text"] = native["display_text"]
    return item


def _function_output_fields(
    output: Any,
    *,
    status: str,
    success: bool | None = None,
    error: Any = None,
) -> dict[str, Any]:
    if success is False:
        status = "failed"
    if status == "failed" and error is None:
        error = (
            output
            if isinstance(output, str)
            else "\n".join(part["text"] for part in output if part["type"] == "input_text")
        ) or None
    return {
        "status": status,
        "output": output if status == "completed" else None,
        "error": error,
    }


class EventProjector:
    """Translate native records for canonical-history reconstruction."""

    def __init__(self, session_id: str, *, source_id: str | None = None):
        self.session_id = session_id
        self.source_id = source_id or session_id
        self._sequence = 0
        self._turn_id: str | None = None
        self._started_at: int | None = None
        self._indices: dict[tuple[str | None, str], int] = {}
        self._items: dict[tuple[str | None, str], dict[str, Any]] = {}
        self._done_items: set[tuple[str | None, str]] = set()
        self._completed_turns: set[str] = set()
        self._parts: set[tuple[str | None, str, int]] = set()
        self._decoders: dict[tuple[str | None, str, str], Any] = {}
        self._last_message: dict[str | None, str] = {}
        self._turn_usage: dict[str, int] | None = None
        self._turn_failure: Failure | None = None
        now = int(time.time())
        self._session: SessionSnapshot = {
            "id": session_id,
            "object": "agent.session",
            "metadata": {},
            "created_at": now,
            "last_active_at": now,
            "status": "idle",
            "required_actions": [],
            "error": None,
            "usage": None,
        }

    def _envelope(self, event_type: str, **fields: Any) -> SessionEvent:
        self._sequence += 1
        return cast(
            SessionEvent,
            {
                "type": event_type,
                "event_id": f"{self.source_id}:{self._sequence}",
                **fields,
            },
        )

    def _event(self, event_type: str, turn_id: str | None, **fields: Any) -> SessionEvent:
        return self._envelope(event_type, session_id=self.session_id, turn_id=turn_id, **fields)

    def _session_event(self, event_type: str) -> SessionEvent:
        return self._envelope(event_type, session=copy.deepcopy(self._session))

    def model_response_declined(self, turn_id: str, response_id: str | None) -> SessionEvent:
        return self._event(
            "agent.session.turn.model_response.decision",
            turn_id,
            response_id=response_id,
            requested="stop",
            applied=False,
            reason="declined",
        )

    def created(self) -> SessionCreatedEvent:
        return cast(SessionCreatedEvent, self._session_event("agent.session.created"))

    def environment_status(
        self,
        status: EnvironmentStatus,
        environment_id: str,
        turn_id: str | None = None,
        error: EnvironmentError | None = None,
    ) -> SessionEvent:
        return self._event(
            f"agent.session.environment.{status}",
            turn_id,
            environment={
                "id": environment_id,
                "type": "self_hosted",
                "status": status,
                "error": error,
            },
        )

    def subagent_turn_completed(self, subagent_id: str, turn_id: str) -> SessionEvent:
        return self._event(
            "agent.session.subagent.turn.completed", turn_id, subagent_id=subagent_id
        )

    def subagent_lifecycle(self, kind: str, subagent: Subagent) -> SessionEvent:
        return self._envelope(f"agent.session.subagent.{kind}", subagent=copy.deepcopy(subagent))

    def set_required_actions(self, actions: list[RequiredAction]) -> list[SessionEvent]:
        blocking = any(
            action["type"] != "user_input" or action["is_blocking"] for action in actions
        )
        status: Literal["idle", "in_progress", "requires_action"] = (
            "requires_action" if blocking else "in_progress" if self._turn_id else "idle"
        )
        if self._session["status"] == status and self._session["required_actions"] == actions:
            return []
        self._session["status"] = status
        self._session["required_actions"] = copy.deepcopy(actions)
        self._session["last_active_at"] = int(time.time())
        return [self._session_event(f"agent.session.{status}")]

    def current_required_actions(self) -> SessionEvent | None:
        if not self._session["required_actions"]:
            return None
        return self._session_event(f"agent.session.{self._session['status']}")

    def error(self, message: str) -> ErrorEvent:
        return cast(
            ErrorEvent,
            self._envelope(
                "error",
                session_id=self.session_id,
                error={
                    "type": "server_error",
                    "code": None,
                    "message": message,
                    "param": None,
                },
            ),
        )

    def _item_fields(self, turn_id: str | None, item_id: str) -> dict[str, Any]:
        key = (turn_id, item_id)
        if key not in self._indices:
            self._indices[key] = sum(turn == turn_id for turn, _ in self._indices)
        return {"item_id": item_id, "output_index": self._indices[key]}

    def _item_added(self, turn_id: str | None, item: dict[str, Any]) -> list[SessionEvent]:
        key = (turn_id, item["id"])
        if key in self._items:
            return []
        self._items[key] = copy.deepcopy(item)
        return [
            self._event(
                "agent.session.turn.item.added",
                turn_id,
                output_index=self._item_fields(turn_id, item["id"])["output_index"],
                item=item,
            )
        ]

    def _part_added(
        self,
        turn_id: str | None,
        item_id: str,
        index: int,
        reasoning: bool,
        text: str = "",
    ) -> list[SessionEvent]:
        key = (turn_id, item_id, index)
        if key in self._parts:
            return []
        self._parts.add(key)
        return [
            self._event(
                "agent.session.turn.reasoning_summary_part.added"
                if reasoning
                else "agent.session.turn.content_part.added",
                turn_id,
                **self._item_fields(turn_id, item_id),
                **{"summary_index" if reasoning else "content_index": index},
                part={
                    "type": "summary_text" if reasoning else "output_text",
                    "text": text,
                },
            )
        ]

    def _flush_command(self, turn_id: str | None, item_id: str) -> list[CommandOutputDeltaEvent]:
        result: list[CommandOutputDeltaEvent] = []
        for key in list(self._decoders):
            turn, item, _ = key
            if (turn, item) != (turn_id, item_id):
                continue
            delta = self._decoders.pop(key).decode(b"", final=True)
            if delta:
                result.append(
                    cast(
                        CommandOutputDeltaEvent,
                        self._event(
                            "agent.output.command_execution_output.delta",
                            turn_id,
                            **self._item_fields(turn_id, item_id),
                            delta=delta,
                        ),
                    )
                )
        return result

    def _item(
        self, native: dict[str, Any], done: bool, turn_id: str | None
    ) -> dict[str, Any] | None:
        kind, item_id = native["type"], native["id"]
        common = {"id": item_id, "turn_id": turn_id}
        if kind == "DynamicToolCall":
            return {
                **common,
                "type": "function_call",
                "call_id": item_id,
                "name": f"{native['namespace']}.{native['tool']}"
                if native.get("namespace")
                else native["tool"],
                "arguments": native["arguments"],
                "status": "failed" if native.get("success") is False else native["status"],
            }
        if turn_id is not None and kind in _EXECUTION_KINDS:
            return _execution_item(native, None, turn_id)
        return project_subagent_item(
            native, done, turn_id, self.source_id
        ) or project_model_output_item(native, done, turn_id)

    def _function_output(
        self,
        native: dict[str, Any],
        turn_id: str | None,
    ) -> SessionEvent:
        output = _response_content(
            [
                content
                for content in native.get("content_items") or []
                if content["type"] in {"inputText", "inputImage", "inputFile"}
            ]
        )
        return self._event(
            "agent.session.turn.item.added",
            turn_id,
            output_index=None,
            item={
                "type": "function_call_output",
                "id": f"{native['id']}:output",
                "turn_id": turn_id,
                "call_id": native["id"],
                **_function_output_fields(
                    output,
                    status=native["status"],
                    success=native.get("success"),
                    error=native.get("error"),
                ),
            },
        )

    def python_tool(
        self,
        message: dict[str, Any],
        response: dict[str, Any] | None = None,
    ) -> list[SessionEvent]:
        done = response is not None
        success = response["success"] if response is not None else None
        return self._item_events(
            {
                "type": "item_completed" if done else "item_started",
                "item": {
                    "type": "DynamicToolCall",
                    "id": message["callId"],
                    "namespace": message.get("namespace"),
                    "tool": message["tool"],
                    "arguments": message["arguments"],
                    "status": ("completed" if success else "failed") if done else "in_progress",
                    "success": success,
                    "content_items": response["contentItems"] if response is not None else None,
                },
            },
            message["turnId"],
        )

    def _item_events(
        self,
        msg: dict[str, Any],
        turn_id: str | None,
    ) -> list[SessionEvent]:
        native = msg["item"]
        done = msg["type"] == "item_completed"
        if native["type"] == "UserMessage":
            key = (turn_id, native["id"])
            if key in self._done_items:
                return []
            self._done_items.add(key)
            content = _response_content(
                [
                    part
                    for part in native["content"]
                    if part["type"] in {"text", "image", "file", "audio"}
                ]
            )
            return [
                self._event(
                    "agent.session.turn.item.added",
                    turn_id,
                    output_index=None,
                    item={
                        "type": "message",
                        "id": native["id"],
                        "turn_id": turn_id,
                        "role": "user",
                        "status": "completed",
                        "phase": None,
                        "content": content,
                    },
                )
            ]
        item = self._item(native, done, turn_id)
        if item is None:
            return []
        key = (turn_id, item["id"])
        if key in self._done_items:
            return []
        initial = copy.deepcopy(item)
        if done:
            initial["status"] = "in_progress"
            if initial["type"] == "message":
                initial["content"] = []
                initial.pop("display_text", None)
            elif initial["type"] == "reasoning":
                initial["summary"] = []
            elif initial["type"] == "command_execution":
                initial.update(output=None, exit_code=None, duration_ms=None)
            elif initial["type"] == "mcp_call":
                initial.update(output=None, error=None)
            elif initial["type"] == "patch_execution":
                initial.update(stdout=None, stderr=None)
        result = self._item_added(turn_id, initial)
        if not done:
            return result
        if native["type"] == "DynamicToolCall":
            result.append(self._function_output(native, turn_id))
        result.extend(self._finish_item(turn_id, item))
        return result

    def _finish_item(self, turn_id: str | None, item: dict[str, Any]) -> list[SessionEvent]:
        item_id = item["id"]
        key = (turn_id, item_id)
        if key in self._done_items:
            return []
        self._done_items.add(key)
        self._items[key] = copy.deepcopy(item)
        fields = self._item_fields(turn_id, item_id)
        result: list[SessionEvent] = []
        if item["type"] in {"message", "reasoning"}:
            reasoning = item["type"] == "reasoning"
            parts = item["summary"] if reasoning else item["content"]
            for index, part in enumerate(parts):
                result.extend(self._part_added(turn_id, item_id, index, reasoning, ""))
                position = {"summary_index" if reasoning else "content_index": index}
                prefix = "reasoning_summary_text" if reasoning else "output_text"
                result.append(
                    self._event(
                        f"agent.session.turn.{prefix}.done",
                        turn_id,
                        **fields,
                        **position,
                        text=part["text"],
                    )
                )
                prefix = "reasoning_summary_part" if reasoning else "content_part"
                result.append(
                    self._event(
                        f"agent.session.turn.{prefix}.done",
                        turn_id,
                        **fields,
                        **position,
                        part=part,
                        **(
                            {"status": "incomplete" if item["status"] == "incomplete" else None}
                            if reasoning
                            else {}
                        ),
                    )
                )
            if not reasoning:
                display_text = item.get("display_text")
                if display_text is None:
                    display_text = "".join(part["text"] for part in parts)
                # Core keeps the previous visible message when this one is blank.
                # Python's strip also removes U+001C through U+001F.
                if display_text.strip() or any(c in display_text for c in "\x1c\x1d\x1e\x1f"):
                    self._last_message[turn_id] = display_text
        flushed = self._flush_command(turn_id, item_id)
        result.extend(flushed)
        if item["type"] == "command_execution" and item["status"] == "incomplete":
            item["output"] = (item["output"] or "") + "".join(event["delta"] for event in flushed)
        result.append(
            self._event(
                "agent.session.turn.item.done",
                turn_id,
                output_index=fields["output_index"],
                item=item,
            )
        )
        return result

    def finish_failed_turn(self) -> list[SessionEvent]:
        """Finish only a received fatal failure when the native stream ends."""
        if self._turn_id is None or self._turn_failure is None:
            return []
        return self._turn_events({"type": "turn_complete"}, self._turn_id)

    def _turn_events(
        self,
        msg: dict[str, Any],
        turn_id: str | None,
        http_status_code: int | None = None,
    ) -> list[SessionEvent]:
        start = msg["type"] in {"task_started", "turn_started"}
        result: list[SessionEvent] = []
        if start:
            self._turn_id = turn_id
            self._started_at = msg.get("started_at") or int(time.time())
            self._turn_usage = None
            self._turn_failure = None
            result.extend(self.set_required_actions(self._session["required_actions"]))
        error = msg.get("error")
        failure = self._turn_failure if turn_id == self._turn_id else None
        if failure is None and error:
            failure = {
                "code": _error_code(error) or "internal_error",
                "message": error["message"],
            }
            status_code = _error_http_status(error, http_status_code)
            if status_code is not None:
                failure["http_status_code"] = status_code
        status = (
            "in_progress"
            if start
            else "failed"
            if failure is not None
            else "cancelled"
            if msg["type"] == "turn_aborted"
            else "completed"
        )
        if not start:
            final_text = msg.get("last_agent_message")
            open_messages = [
                item["id"]
                for key, item in self._items.items()
                if key[0] == turn_id and key not in self._done_items and item["type"] == "message"
            ]
            for (turn, _), item in list(self._items.items()):
                if turn == turn_id and (turn, item["id"]) not in self._done_items:
                    unfinished = copy.deepcopy(item)
                    unfinished["status"] = "incomplete"
                    if (
                        status == "completed"
                        and final_text is not None
                        and open_messages
                        and item["id"] == open_messages[-1]
                    ):
                        if any("annotations" in part for part in unfinished["content"]):
                            unfinished["display_text"] = final_text
                        else:
                            unfinished["content"] = [{"type": "output_text", "text": final_text}]
                        unfinished["status"] = "completed"
                    result.extend(self._finish_item(turn_id, unfinished))
            if (
                status == "completed"
                and final_text is not None
                and self._last_message.get(turn_id) != final_text
            ):
                result.extend(
                    self._item_events(
                        {
                            "type": "item_completed",
                            "item": {
                                "type": "AgentMessage",
                                "id": f"{turn_id}:final",
                                "content": [{"type": "Text", "text": final_text}],
                                "phase": "final_answer",
                            },
                        },
                        turn_id,
                    )
                )
        usage = _usage(self._turn_usage) if self._turn_usage is not None else None
        started_at = msg.get("started_at", self._started_at)
        result.append(
            self._event(
                f"agent.session.turn.{status}",
                turn_id,
                turn={
                    "id": turn_id,
                    "object": "agent.session.turn",
                    "session_id": self.session_id,
                    "status": status,
                    "created_at": started_at or int(time.time()),
                    "started_at": started_at,
                    "completed_at": None if start else msg.get("completed_at", int(time.time())),
                    "error": failure,
                    "usage": usage,
                },
                **({"usage": usage} if not start else {}),
            )
        )
        if not start:
            if turn_id is not None:
                self._completed_turns.add(turn_id)
            self._indices = {
                key: value for key, value in self._indices.items() if key[0] != turn_id
            }
            self._items = {key: value for key, value in self._items.items() if key[0] != turn_id}
            self._done_items = {key for key in self._done_items if key[0] != turn_id}
            self._parts = {key for key in self._parts if key[0] != turn_id}
            self._last_message.pop(turn_id, None)
            if self._turn_id == turn_id:
                self._turn_id = None
                self._started_at = None
                self._turn_usage = None
                self._turn_failure = None
            result.extend(
                self.set_required_actions(
                    [
                        action
                        for action in self._session["required_actions"]
                        if action.get("subagent_id") is not None
                        or action.get("turn_id") not in {None, turn_id}
                    ]
                )
            )
        return result

    def project(
        self,
        event: NativeEvent | dict[str, Any],
        *,
        http_status_code: int | None = None,
    ) -> list[SessionEvent]:
        msg = cast(dict[str, Any], event["msg"])
        kind = msg["type"]
        turn_id = msg.get("turn_id", msg.get("turnId"))
        if not turn_id and kind in {
            "task_started",
            "turn_started",
            "task_complete",
            "turn_complete",
            "turn_aborted",
            "exec_command_output_delta",
            "error",
            "raw_response_completed",
        }:
            turn_id = event.get("id") or self._turn_id
        elif not turn_id:
            turn_id = self._turn_id
        if turn_id in self._completed_turns and kind in {
            "task_started",
            "turn_started",
            "task_complete",
            "turn_complete",
            "turn_aborted",
            "item_started",
            "item_completed",
            "agent_message_content_delta",
            "reasoning_content_delta",
            "exec_command_output_delta",
        }:
            return []
        if kind in {
            "task_started",
            "turn_started",
            "task_complete",
            "turn_complete",
            "turn_aborted",
        }:
            return self._turn_events(msg, turn_id, http_status_code)
        if kind in {"item_started", "item_completed"}:
            return self._item_events(msg, turn_id)
        if kind in {"agent_message_content_delta", "reasoning_content_delta"}:
            reasoning = kind == "reasoning_content_delta"
            item_id = msg["item_id"]
            index = msg.get("summary_index", 0) if reasoning else 0
            initial = self._item(
                {"type": "Reasoning" if reasoning else "AgentMessage", "id": item_id},
                False,
                turn_id,
            )
            assert initial is not None
            result = self._item_added(turn_id, initial)
            if (turn_id, item_id) in self._done_items:
                return []
            parts = self._items[(turn_id, item_id)]["summary" if reasoning else "content"]
            while len(parts) <= index:
                parts.append({"type": "summary_text" if reasoning else "output_text", "text": ""})
            # Deltas carry visible text. Annotated parts retain the original text.
            if reasoning or not any("annotations" in part for part in parts):
                parts[index]["text"] += msg["delta"]
            result.extend(self._part_added(turn_id, item_id, index, reasoning))
            result.append(
                self._event(
                    "agent.session.turn.reasoning_summary_text.delta"
                    if reasoning
                    else "agent.session.turn.output_text.delta",
                    turn_id,
                    **self._item_fields(turn_id, item_id),
                    **{"summary_index" if reasoning else "content_index": index},
                    delta=msg["delta"],
                )
            )
            return result
        if kind == "exec_command_output_delta":
            item_id, stream = msg["call_id"], msg["stream"]
            if (turn_id, item_id) not in self._items or (
                turn_id,
                item_id,
            ) in self._done_items:
                return []
            key = (turn_id, item_id, stream)
            decoder = self._decoders.setdefault(
                key, codecs.getincrementaldecoder("utf-8")("replace")
            )
            delta = decoder.decode(base64.b64decode(msg["chunk"]))
            current = self._items[(turn_id, item_id)]
            current["output"] = (current.get("output") or "") + delta
            return (
                [
                    self._event(
                        "agent.output.command_execution_output.delta",
                        turn_id,
                        **self._item_fields(turn_id, item_id),
                        delta=delta,
                    )
                ]
                if delta
                else []
            )
        if kind == "token_count":
            info = msg.get("info")
            if info and info.get("total_token_usage"):
                self._session["usage"] = _usage(info["total_token_usage"])
            return []
        if kind == "raw_response_completed":
            usage = msg.get("token_usage")
            if usage and turn_id == self._turn_id:
                if self._turn_usage is None:
                    self._turn_usage = {}
                for key, value in usage.items():
                    if isinstance(value, int):
                        self._turn_usage[key] = self._turn_usage.get(key, 0) + value
            return []
        if kind == "error":
            error_event = self.error(msg["message"])
            error_event["error"]["code"] = _error_code(msg)
            status_code = _error_http_status(msg, http_status_code)
            if status_code is not None:
                error_event["error"]["http_status_code"] = status_code
            if (
                self._turn_id is not None
                and turn_id == self._turn_id
                and event.get("error_affects_turn_status", True)
                and self._turn_failure is None
            ):
                self._turn_failure = {
                    "code": error_event["error"]["code"] or "internal_error",
                    "message": error_event["error"]["message"],
                }
                if status_code is not None:
                    self._turn_failure["http_status_code"] = status_code
            return [error_event]
        return []


_preserve_module_identity(globals(), "agents._projection")
