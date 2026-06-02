"""
A2A ↔ OpenAI Agents SDK message format converter.

This module handles bidirectional conversion between the A2A protocol types
(protobuf-based Message, Part, Task, Artifact) and the OpenAI Agents SDK types
(TResponseInputItem, RunResult, StreamEvent).

The converter is designed as pure functions with no side effects, making it
easy to test and compose. All protobuf interactions are isolated to this module.
"""

from __future__ import annotations

import base64
import dataclasses as _dataclasses
import json as json_module
import uuid
from typing import TYPE_CHECKING, Any

from google.protobuf.timestamp_pb2 import Timestamp

from agents.items import TResponseInputItem

if TYPE_CHECKING:
    from a2a.types.a2a_pb2 import (
        Artifact,
        Message,
        Part,
        Task,
        TaskStatus,
    )
    from a2a.server.agent_execution.context import RequestContext  # type: ignore[import-untyped]
    from agents.result import RunResult
    from agents.stream_events import StreamEvent


# ---------------------------------------------------------------------------
# A2A → OpenAI conversion
# ---------------------------------------------------------------------------

# Role mapping enums are defined in the A2A proto (0=UNSPECIFIED, 1=USER, 2=AGENT).
# We handle the int values to avoid coupling to the generated enum.
_A2A_ROLE_USER = 1
_A2A_ROLE_AGENT = 2


def a2a_message_to_openai_input_items(
    message: Message,
    *,
    include_role: bool = True,
) -> list[TResponseInputItem]:
    """Convert a single A2A ``Message`` to OpenAI ``TResponseInputItem`` dicts.

    Each ``Part`` of the message becomes one input item. Text parts become
    ``message``-role items; file/data parts become ``file`` or ``image`` items
    when the MIME type is known.

    Args:
        message: The A2A protobuf ``Message`` to convert.
        include_role: When True (default), the message's ``role`` field is used
            as the OpenAI item role. When False, the role is omitted so the
            caller can assign it externally.

    Returns:
        A list of ``TResponseInputItem`` dicts ready to pass to ``Runner.run()``.
    """
    items: list[TResponseInputItem] = []

    for part in message.parts:
        item = _convert_single_part(part)
        if item is None:
            continue
        if include_role:
            item["role"] = _a2a_role_to_openai_role(message.role)
        items.append(item)

    return items


def a2a_history_to_openai_input_items(
    history: list[Message],
) -> list[TResponseInputItem]:
    """Convert a list of A2A ``Message`` objects (e.g. ``Task.history``)
    into a flat list of OpenAI input items, preserving role information
    from each message.

    Args:
        history: The message history from an A2A ``Task``.

    Returns:
        A flat list of ``TResponseInputItem`` dicts.
    """
    items: list[TResponseInputItem] = []
    for message in history:
        items.extend(a2a_message_to_openai_input_items(message, include_role=True))
    return items


def a2a_context_to_openai_input(
    context: RequestContext,
) -> list[TResponseInputItem]:
    """Build the full OpenAI input from an A2A ``RequestContext``.

    This merges:
    1. The current incoming message (if any).
    2. The task history from the current task (if any).
    3. History from related tasks (if any), prefixed with a system note.

    Args:
        context: The A2A server ``RequestContext``.

    Returns:
        A flat list of ``TResponseInputItem`` dicts representing the full
        conversation context for the agent to process.
    """
    items: list[TResponseInputItem] = []

    # 1. Current message
    if context.message is not None:
        items.extend(
            a2a_message_to_openai_input_items(context.message, include_role=True)
        )

    # 2. Task history (existing conversation)
    if context.current_task is not None and context.current_task.history:
        history_items = a2a_history_to_openai_input_items(
            list(context.current_task.history)
        )
        # Avoid duplicating the current message if it's already in history
        if items and history_items:
            existing_content = {_item_content_hash(i) for i in items}
            for hi in history_items:
                if _item_content_hash(hi) not in existing_content:
                    items.append(hi)
        else:
            items.extend(history_items)

    # 3. Related tasks
    for related_task in context.related_tasks:
        if related_task.history:
            note: TResponseInputItem = {
                "role": "user",
                "content": _format_related_task_note(related_task),
            }
            items.append(note)
            items.extend(a2a_history_to_openai_input_items(list(related_task.history)))

    return items


# ---------------------------------------------------------------------------
# OpenAI → A2A conversion
# ---------------------------------------------------------------------------


def openai_final_output_to_artifacts(
    final_output: Any,
    *,
    artifact_id: str | None = None,
    artifact_name: str = "output",
) -> list[Artifact]:
    """Convert an OpenAI agent's ``final_output`` into one or more A2A ``Artifact``
    objects.

    - ``str`` output → single ``Artifact`` with a text part.
    - ``dict`` / Pydantic model → single ``Artifact`` with a JSON ``data`` part.
    - ``list`` → one ``Artifact`` per element.
    - ``None`` → empty list.

    Args:
        final_output: The ``RunResult.final_output`` value.
        artifact_id: Optional artifact ID; auto-generated if omitted.
        artifact_name: Human-readable label for the artifact.

    Returns:
        A list of A2A ``Artifact`` protobuf messages.
    """
    from a2a.types.a2a_pb2 import Artifact, Part

    if final_output is None:
        return []

    if isinstance(final_output, list):
        return [
            a
            for item in final_output
            for a in openai_final_output_to_artifacts(
                item,
                artifact_id=None,
                artifact_name=artifact_name,
            )
        ]

    artifact_id = artifact_id or f"artifact-{uuid.uuid4().hex[:12]}"

    if isinstance(final_output, str):
        text_part = Part(text=final_output)
        # Use media_type to help clients interpret the content.
        text_part.media_type = "text/plain"
        return [
            Artifact(
                artifact_id=artifact_id,
                name=artifact_name,
                parts=[text_part],
            )
        ]

    # dict, Pydantic model, dataclass, etc.
    try:
        json_str = _serialize_to_json(final_output)
        data_part = Part()
        data_part.media_type = "application/json"
        # data is a google.protobuf.Value; we assign the raw JSON string as text
        # because Part.data expects a Value protobuf, not a plain string.
        # Text is the most compatible single-part representation.
        text_part = Part(text=json_str)
        text_part.media_type = "application/json"
        return [
            Artifact(
                artifact_id=artifact_id,
                name=artifact_name,
                parts=[text_part],
            )
        ]
    except Exception:
        text_part = Part(text=str(final_output))
        text_part.media_type = "text/plain"
        return [
            Artifact(
                artifact_id=artifact_id,
                name=artifact_name,
                parts=[text_part],
            )
        ]


def openai_items_to_a2a_messages(
    items: list[TResponseInputItem],
    *,
    context_id: str | None = None,
    task_id: str | None = None,
) -> list[Message]:
    """Convert a list of OpenAI input/output items into A2A ``Message`` objects.

    Each item becomes a single ``Message``. The item's ``role`` determines the
    A2A ``Role``. Tool call / tool output items are represented as data parts.

    Args:
        items: The items to convert (e.g. ``RunResult.new_items``).
        context_id: Optional context ID to set on every message.
        task_id: Optional task ID to set on every message.

    Returns:
        A list of A2A ``Message`` protobuf messages.
    """
    from a2a.types.a2a_pb2 import Message, Part

    messages: list[Message] = []
    for item in items:
        part = _openai_item_to_part(item)
        if part is None:
            continue

        role = _openai_role_to_a2a_role(item.get("role", "user"))

        message = Message(
            message_id=f"msg-{uuid.uuid4().hex[:12]}",
            role=role,
            parts=[part],
        )
        if context_id:
            message.context_id = context_id
        if task_id:
            message.task_id = task_id

        messages.append(message)

    return messages


def openai_run_result_to_task(
    result: RunResult,
    *,
    task_id: str,
    context_id: str | None = None,
) -> Task:
    """Build a complete A2A ``Task`` from an OpenAI ``RunResult``.

    The task will be in ``TASK_STATE_COMPLETED`` and contain:
    - Full conversation history in ``history``.
    - The final output as ``artifacts``.

    Args:
        result: The completed ``RunResult`` from ``Runner.run()``.
        task_id: The A2A task ID to assign.
        context_id: Optional context ID.

    Returns:
        An A2A ``Task`` protobuf message in completed state.
    """
    from a2a.types.a2a_pb2 import (
        Artifact,
        Task,
        TaskState,
        TaskStatus,
    )

    # Build history from both input and new items.
    # RunResult.new_items contains RunItem objects; convert them to
    # TResponseInputItem dicts so downstream converters can handle them.
    from agents.items import RunItemBase

    input_items: list[TResponseInputItem] = []
    for item in result.new_items:
        if isinstance(item, RunItemBase):
            input_items.append(item.to_input_item())
        else:
            input_items.append(item)  # type: ignore[arg-type]

    history_messages = openai_items_to_a2a_messages(
        input_items,
        context_id=context_id,
        task_id=task_id,
    )

    # Build artifacts from final output
    artifacts = openai_final_output_to_artifacts(result.final_output)

    status_message = _make_status_message(
        text="Task completed successfully.",
        context_id=context_id,
        task_id=task_id,
    )

    timestamp = Timestamp()
    timestamp.GetCurrentTime()

    status = TaskStatus(
        state=TaskState.TASK_STATE_COMPLETED,
        message=status_message,
        timestamp=timestamp,
    )

    task = Task(
        id=task_id,
        status=status,
        artifacts=artifacts,
        history=history_messages,
    )
    if context_id:
        task.context_id = context_id

    return task


def openai_error_to_failed_task(
    error: Exception,
    *,
    task_id: str,
    context_id: str | None = None,
    history: list[Message] | None = None,
) -> Task:
    """Build an A2A ``Task`` in ``TASK_STATE_FAILED`` from an exception.

    Args:
        error: The exception that caused the failure.
        task_id: The A2A task ID.
        context_id: Optional context ID.
        history: Optional message history accumulated before the failure.

    Returns:
        An A2A ``Task`` in failed state.
    """
    from a2a.types.a2a_pb2 import Task, TaskState, TaskStatus

    error_text = f"Task failed: {type(error).__name__}: {error}"
    status_message = _make_status_message(
        text=error_text,
        context_id=context_id,
        task_id=task_id,
    )

    timestamp = Timestamp()
    timestamp.GetCurrentTime()

    status = TaskStatus(
        state=TaskState.TASK_STATE_FAILED,
        message=status_message,
        timestamp=timestamp,
    )

    task = Task(
        id=task_id,
        status=status,
        history=list(history) if history else [],
    )
    if context_id:
        task.context_id = context_id

    return task


def openai_stream_event_to_task_status(
    event: StreamEvent,
    *,
    task_id: str,
    context_id: str | None = None,
) -> TaskStatus | None:
    """Convert a single OpenAI ``StreamEvent`` into an A2A ``TaskStatus``.

    Returns ``None`` for events that do not represent a task state change
    (e.g. raw model deltas that should be aggregated).

    Args:
        event: The streaming event from ``RunResultStreaming.stream_events()``.
        task_id: The A2A task ID.
        context_id: Optional context ID.

    Returns:
        An ``TaskStatus`` or ``None``.
    """
    from a2a.types.a2a_pb2 import TaskState, TaskStatus

    from agents.stream_events import (
        AgentUpdatedStreamEvent,
        RawResponsesStreamEvent,
        RunItemStreamEvent,
    )

    timestamp = Timestamp()
    timestamp.GetCurrentTime()

    if isinstance(event, RunItemStreamEvent):
        status_message = _make_status_message(
            text=f"Agent produced: {event.name}",
            context_id=context_id,
            task_id=task_id,
        )
        return TaskStatus(
            state=TaskState.TASK_STATE_WORKING,
            message=status_message,
            timestamp=timestamp,
        )

    if isinstance(event, AgentUpdatedStreamEvent):
        status_message = _make_status_message(
            text=f"Agent switched to: {event.new_agent.name}",
            context_id=context_id,
            task_id=task_id,
        )
        return TaskStatus(
            state=TaskState.TASK_STATE_WORKING,
            message=status_message,
            timestamp=timestamp,
        )

    if isinstance(event, RawResponsesStreamEvent):
        # Raw model events don't map cleanly to task states; skip.
        return None

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _convert_single_part(part: Part) -> TResponseInputItem | None:
    """Convert a single A2A ``Part`` to an OpenAI input item dict."""
    content_type = part.WhichOneof("content")
    if content_type is None:
        return None

    if content_type == "text":
        return {"content": part.text}

    if content_type == "url":
        return {"content": _format_url_content(part.url)}

    if content_type == "raw":
        return _convert_raw_part(part)

    if content_type == "data":
        return _convert_data_part(part)

    return None


def _convert_raw_part(part: Part) -> TResponseInputItem | None:
    """Convert a raw bytes Part to an image or file item based on media_type."""
    media = (part.media_type or "").lower()

    if media.startswith("image/"):
        b64 = base64.b64encode(part.raw).decode("ascii")
        return {
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{b64}"},
                }
            ]
        }

    # Generic file: attach as text description with metadata
    return {
        "content": (
            f"[Attached file: {part.filename or 'unnamed'} "
            f"({media or 'application/octet-stream'}, "
            f"{len(part.raw)} bytes)]"
        )
    }


def _convert_data_part(part: Part) -> TResponseInputItem | None:
    """Convert a structured data Part to an OpenAI input item."""
    try:
        data_dict = json_format.MessageToDict(part.data)
        json_str = json_module.dumps(data_dict, ensure_ascii=False)
        return {"content": json_str}
    except Exception:
        return {"content": str(part.data)}


def _openai_item_to_part(item: TResponseInputItem) -> Part | None:
    """Convert a single OpenAI item to an A2A ``Part``."""
    from a2a.types.a2a_pb2 import Part

    content = item.get("content")

    if content is None:
        return None

    if isinstance(content, str):
        text_part = Part(text=content)
        text_part.media_type = "text/plain"
        return text_part

    if isinstance(content, list):
        # Multi-modal content: extract text portions; preserve the first
        # image URL as a note since the A2A text part is the primary medium.
        texts: list[str] = []
        image_urls: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    image_urls.append(
                        block.get("image_url", {}).get("url", "")
                    )
        if image_urls:
            texts.append(f"[Attached image(s): {', '.join(image_urls[:3])}]")

        if texts:
            text_part = Part(text="\n".join(texts))
            text_part.media_type = "text/plain"
            return text_part

    # Fallback: stringify
    text_part = Part(text=str(content))
    text_part.media_type = "text/plain"
    return text_part


def _a2a_role_to_openai_role(a2a_role: int) -> str:
    """Map A2A Role enum value to OpenAI role string."""
    if a2a_role == _A2A_ROLE_USER:
        return "user"
    if a2a_role == _A2A_ROLE_AGENT:
        return "assistant"
    return "user"


def _openai_role_to_a2a_role(openai_role: object) -> int:
    """Map OpenAI role string to A2A Role enum value."""
    role_str = str(openai_role).lower()
    if role_str in ("assistant", "agent", "model"):
        return _A2A_ROLE_AGENT
    return _A2A_ROLE_USER


def _format_url_content(url: str) -> str:
    """Format a URL part as text content for the model."""
    return f"[URL: {url}]"


def _serialize_to_json(obj: Any) -> str:
    """Serialize an arbitrary object to a JSON string, handling Pydantic models,
    dataclasses, and plain dicts."""
    try:
        from pydantic import BaseModel

        if isinstance(obj, BaseModel):
            return obj.model_dump_json()
    except ImportError:
        pass

    if _dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_module.dumps(_dataclasses.asdict(obj), default=str)

    if isinstance(obj, dict):
        return json_module.dumps(obj, default=str)

    return json_module.dumps({"value": obj}, default=str)


def _item_content_hash(item: TResponseInputItem) -> int:
    """Fast content-based hash for deduplication."""
    content = item.get("content", "")
    role = item.get("role", "")
    return hash(f"{role}:{content}")


def _format_related_task_note(task: Task) -> str:
    """Format a system note describing a related task's context."""
    task_id = getattr(task, "id", "unknown")
    task_state = _task_state_name(task)
    return (
        f"[Related task {task_id} (status: {task_state}) "
        f"provides additional context below:]"
    )


def _task_state_name(task: Task) -> str:
    """Human-readable task state string."""
    try:
        from a2a.types.a2a_pb2 import TaskState

        state_value = task.status.state
        return TaskState.Name(state_value)
    except (AttributeError, ValueError):
        return "unknown"


def _make_status_message(
    text: str,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    """Create a small status ``Message`` for use inside ``TaskStatus``."""
    from a2a.types.a2a_pb2 import Message, Part

    text_part = Part(text=text)
    text_part.media_type = "text/plain"

    message = Message(
        message_id=f"status-{uuid.uuid4().hex[:12]}",
        role=_A2A_ROLE_AGENT,
        parts=[text_part],
    )
    if context_id:
        message.context_id = context_id
    if task_id:
        message.task_id = task_id
    return message
