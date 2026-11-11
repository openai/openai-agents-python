"""Passive Vercel UI Message Stream v1 projection for native turns."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from ._mcp_errors import _protect_method
from ._public import preserve_module_identity as _preserve_module_identity
from .observation import (
    EventGap,
    OutputTextDelta,
    ToolFinished,
    ToolStarted,
    TurnStarted,
)
from .session import Session, TurnStoppedError
from .turn import Turn

__all__ = ["vercel_stream"]


def _sse(chunk: dict[str, Any]) -> bytes:
    return (
        "data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode()


async def vercel_stream(
    turn: Turn[str], *, session: Session[Any, Any], include_tool_data: bool = False
) -> AsyncGenerator[bytes, None]:
    """Yield Vercel UI Message Stream v1 SSE bytes for one plain-text turn.

    Set Content-Type: text/event-stream and x-vercel-ai-ui-message-stream: v1
    on the application-owned response. Submit with default/str output, without
    prompted or structured output overrides. This fresh stream cannot resume a
    client transcript or span steering boundaries. Missing observations produce
    resync_required; use an authorized Session history view to recover.

    Tool arguments/results are sensitive and omitted by default. With
    include_tool_data=True, complete retained function/MCP input and output are
    emitted after settlement, keyed by canonical call ID. Core may have bounded
    retained MCP output; this adapter cannot recover omitted remote bytes.
    State is bounded to 256 text parts/tool calls and 1 MiB each of text/tool data.
    Closing this iterator releases only its reader, never the turn or Session.
    """
    if type(include_tool_data) is not bool:
        raise TypeError("include_tool_data must be a bool")
    if session.id != turn.session_id:
        raise ValueError("Pass this Turn's owning Session")
    events = turn.events()
    # Observations do not promise a delta for a done-only response. Retain
    # shown text by real item ID so success cannot silently omit the result.
    opened: dict[str, list[str]] = {}
    started: str | None = None
    saw_gap = False
    text_size = 0
    try:
        yield _sse({"type": "start"})
        async for event in events:
            if (
                isinstance(event, TurnStarted)
                and started is None
                and event.submission_id == turn.submission_id
            ):
                started = event.turn_id
                continue
            if isinstance(event, EventGap) and not started:
                saw_gap = True
                # All earlier loss is irrelevant only if this turn's start
                # remains available. Never display a suffix without it.
                continue
            if not started or isinstance(event, (EventGap, TurnStarted)):
                for item_id in opened:
                    yield _sse({"type": "text-end", "id": item_id})
                yield _sse({"type": "error", "errorText": "resync_required"})
                yield b"data: [DONE]\n\n"
                return
            if isinstance(event, OutputTextDelta):
                text_size += len(event.delta.encode("utf-8"))
                if text_size > 1_048_576 or (event.item_id not in opened and len(opened) >= 256):
                    for item_id in opened:
                        yield _sse({"type": "text-end", "id": item_id})
                    yield _sse({"type": "error", "errorText": "resync_required"})
                    yield b"data: [DONE]\n\n"
                    return
                if event.item_id not in opened:
                    opened[event.item_id] = []
                    yield _sse({"type": "text-start", "id": event.item_id})
                opened[event.item_id].append(event.delta)
                yield _sse({"type": "text-delta", "id": event.item_id, "delta": event.delta})
            elif isinstance(event, (ToolStarted, ToolFinished)):
                progress: dict[str, Any] = {
                    "type": event.type,
                    "call_id": event.call_id,
                    "name": event.name,
                    "namespace": event.namespace,
                    "family": event.family,
                }
                if isinstance(event, ToolFinished):
                    progress["status"] = event.status
                yield _sse({"type": "data-tool-progress", "data": progress, "transient": True})
        for item_id in opened:
            yield _sse({"type": "text-end", "id": item_id})
        # After the stream ends, an unassigned ID means this Turn never
        # published an SDK start, so the gap cannot have hidden its start.
        if not started and saw_gap and turn.turn_id is not None:
            yield _sse({"type": "error", "errorText": "resync_required"})
        else:
            try:
                completed = await turn.get_result()
            except TurnStoppedError:
                yield _sse({"type": "abort"})
            except Exception:
                yield _sse({"type": "error", "errorText": "turn_failed"})
            else:
                if not isinstance(completed.final_output, str):
                    yield _sse({"type": "error", "errorText": "plain_text_required"})
                    yield b"data: [DONE]\n\n"
                    return
                if include_tool_data and started:
                    try:
                        frames = await _tool_frames(session, turn.child_id, started)
                    except Exception:
                        yield _sse({"type": "error", "errorText": "resync_required"})
                        yield b"data: [DONE]\n\n"
                        return
                    for frame in frames:
                        yield frame
                final_id = None
                unphased_id = None
                recorded_text: dict[str, str] = {}
                if started:
                    try:
                        async for item in session.items(subagent_id=turn.child_id):
                            if (
                                item["type"] == "message"
                                and item["role"] == "assistant"
                                and item["turn_id"] == started
                                and item["status"] == "completed"
                            ):
                                display_text = item.get("display_text")
                                recorded_text[item["id"]] = (
                                    display_text
                                    if display_text is not None
                                    else "".join(part.get("text", "") for part in item["content"])
                                )
                                if (
                                    len(recorded_text) > 256
                                    or sum(
                                        len(text.encode("utf-8")) for text in recorded_text.values()
                                    )
                                    > 1_048_576
                                ):
                                    raise ValueError("UI history projection exceeds its bound")
                                if item["phase"] == "final_answer":
                                    final_id = item["id"]
                                elif item["phase"] is None:
                                    unphased_id = item["id"]
                                    # Rust treats U+001C..U+001F as nonblank.
                                    if re.search(r"\S|[\x1c-\x1f]", recorded_text[item["id"]]):
                                        final_id = item["id"]
                    except Exception:
                        # Unavailable identity cannot establish a complete display.
                        yield _sse({"type": "error", "errorText": "resync_required"})
                        yield b"data: [DONE]\n\n"
                        return
                final_id = final_id or unphased_id
                if (
                    started
                    and all(
                        "".join(parts) == recorded_text.get(item_id)
                        for item_id, parts in opened.items()
                    )
                    and all(
                        not text or text == "".join(opened.get(item_id, []))
                        for item_id, text in recorded_text.items()
                    )
                    and (
                        (
                            not completed.final_output
                            and not any(any(parts) for parts in opened.values())
                        )
                        or (
                            final_id is not None
                            and final_id in opened
                            and (
                                "".join(opened[final_id]) == completed.final_output
                                or (
                                    completed.final_output == ""
                                    # Rust whitespace excludes Python's U+001C..U+001F.
                                    and re.sub(
                                        r"^[^\S\x1c-\x1f]+|[^\S\x1c-\x1f]+\Z",
                                        "",
                                        "".join(opened[final_id]),
                                    )
                                    == ""
                                )
                            )
                        )
                    )
                ):
                    yield _sse({"type": "finish"})
                else:
                    # No public item ID exists for text seen only at completion.
                    # Let the application choose its authorized history view.
                    yield _sse({"type": "error", "errorText": "resync_required"})
        yield b"data: [DONE]\n\n"
    except Exception:
        for item_id in opened:
            yield _sse({"type": "text-end", "id": item_id})
        yield _sse({"type": "error", "errorText": "turn_failed"})
        yield b"data: [DONE]\n\n"
    finally:
        await events.aclose()


async def _tool_frames(
    session: Session[Any, Any], child_id: str | None, turn_id: str
) -> list[bytes]:
    """Join retained calls and results from this exact native turn only."""
    calls: dict[str, Mapping[str, Any]] = {}
    outputs: dict[str, Mapping[str, Any]] = {}
    size = 0
    async for item in session.items(subagent_id=child_id):
        if item["turn_id"] != turn_id or (
            item["type"] != "function_call"
            and item["type"] != "function_call_output"
            and item["type"] != "mcp_call"
        ):
            continue
        size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if size > 1_048_576:
            raise ValueError("UI tool projection exceeds its bound")
        call_id = item.get("call_id", item["id"])
        if item["type"] == "function_call_output":
            if call_id in outputs:
                raise ValueError("Ambiguous retained tool result")
            outputs[call_id] = item
        else:
            previous = calls.get(call_id)
            if previous is not None and previous["type"] == item["type"]:
                raise ValueError("Ambiguous retained tool call")
            # Native MCP completion enriches its model function-call record.
            if previous is None or item["type"] == "mcp_call":
                calls[call_id] = item
        if len(calls) > 256 or len(outputs) > 256:
            raise ValueError("UI tool projection exceeds its bound")
    frames: list[bytes] = []
    for call_id, call in calls.items():
        retained_output = outputs.pop(call_id, None)
        output = call if call["type"] == "mcp_call" else retained_output
        if output is None or output["status"] == "in_progress":
            raise ValueError("Missing completed tool result")
        arguments = call["arguments"]
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass  # Custom tool input can be plain text.
        name = call["name"]
        if call["type"] == "mcp_call":
            name = f"{call['server_label']}.{name}"
        frames.append(
            _sse(
                {
                    "type": "tool-input-available",
                    "toolCallId": call_id,
                    "toolName": name,
                    "input": arguments,
                    "dynamic": True,
                }
            )
        )
        error = output.get("error")
        if error is not None or output["status"] != "completed":
            frames.append(
                _sse(
                    {
                        "type": "tool-output-error",
                        "toolCallId": call_id,
                        "errorText": str(error or "Tool did not complete"),
                        "dynamic": True,
                    }
                )
            )
        else:
            frames.append(
                _sse(
                    {
                        "type": "tool-output-available",
                        "toolCallId": call_id,
                        "output": output["output"],
                        "dynamic": True,
                    }
                )
            )
    if outputs:
        raise ValueError("Tool result has no retained call")
    return frames


def _stream_mcp(owner: Any) -> tuple[Any, ...]:
    if isinstance(owner, Turn):
        owner = owner._session
    if not isinstance(owner, Session):
        return ()
    opening = owner._opening_agent
    return (opening, *(opening.tools if opening is not None else ()), *owner._mcp_tools)


# Keep the public declaration free of the private boundary implementation.
globals()["vercel_stream"] = _protect_method(
    vercel_stream,
    _stream_mcp,
    owner_keywords=("turn", "session"),
    preserve_generator_protocol=True,
)
_preserve_module_identity(globals(), "agents.ui")
