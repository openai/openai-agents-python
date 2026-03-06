from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Union, cast

from typing_extensions import Literal, TypeAlias

from .items import ThreadItem, coerce_thread_item
from .payloads import _DictLike

# Event payloads emitted by the Codex CLI JSONL stream.


@dataclass(frozen=True)
class ThreadStartedEvent(_DictLike):
    thread_id: str
    type: Literal["thread.started"] = field(default="thread.started", init=False)


@dataclass(frozen=True)
class TurnStartedEvent(_DictLike):
    type: Literal["turn.started"] = field(default="turn.started", init=False)


@dataclass(frozen=True)
class Usage(_DictLike):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class TurnCompletedEvent(_DictLike):
    usage: Usage | None = None
    type: Literal["turn.completed"] = field(default="turn.completed", init=False)


@dataclass(frozen=True)
class ThreadError(_DictLike):
    message: str


@dataclass(frozen=True)
class TurnFailedEvent(_DictLike):
    error: ThreadError
    type: Literal["turn.failed"] = field(default="turn.failed", init=False)


@dataclass(frozen=True)
class ItemStartedEvent(_DictLike):
    item: ThreadItem
    type: Literal["item.started"] = field(default="item.started", init=False)


@dataclass(frozen=True)
class ItemUpdatedEvent(_DictLike):
    item: ThreadItem
    type: Literal["item.updated"] = field(default="item.updated", init=False)


@dataclass(frozen=True)
class ItemCompletedEvent(_DictLike):
    item: ThreadItem
    type: Literal["item.completed"] = field(default="item.completed", init=False)


@dataclass(frozen=True)
class ThreadErrorEvent(_DictLike):
    message: str
    type: Literal["error"] = field(default="error", init=False)


@dataclass(frozen=True)
class _UnknownThreadEvent(_DictLike):
    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)


ThreadEvent: TypeAlias = Union[
    ThreadStartedEvent,
    TurnStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    ItemStartedEvent,
    ItemUpdatedEvent,
    ItemCompletedEvent,
    ThreadErrorEvent,
    _UnknownThreadEvent,
]


def _coerce_thread_error(raw: ThreadError | Mapping[str, Any]) -> ThreadError:
    if isinstance(raw, ThreadError):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("ThreadError must be a mapping.")
    return ThreadError(message=cast(str, raw.get("message", "")))


def coerce_usage(raw: Usage | Mapping[str, Any]) -> Usage:
    if isinstance(raw, Usage):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("Usage must be a mapping.")
    return Usage(
        input_tokens=cast(int, raw["input_tokens"]),
        cached_input_tokens=cast(int, raw["cached_input_tokens"]),
        output_tokens=cast(int, raw["output_tokens"]),
    )


def coerce_thread_event(raw: ThreadEvent | Mapping[str, Any]) -> ThreadEvent:
    if isinstance(raw, _DictLike):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("Thread event payload must be a mapping.")

    event_type = raw.get("type")
    if event_type == "thread.started":
        return ThreadStartedEvent(thread_id=cast(str, raw["thread_id"]))
    if event_type == "turn.started":
        return TurnStartedEvent()
    if event_type == "turn.completed":
        usage_raw = raw.get("usage")
        usage = coerce_usage(cast(Mapping[str, Any], usage_raw)) if usage_raw is not None else None
        return TurnCompletedEvent(usage=usage)
    if event_type == "turn.failed":
        error_raw = raw.get("error", {})
        error = _coerce_thread_error(cast(Mapping[str, Any], error_raw))
        return TurnFailedEvent(error=error)
    if event_type == "item.started":
        item_raw = raw.get("item")
        item = (
            coerce_thread_item(cast(Union[ThreadItem, Mapping[str, Any]], item_raw))
            if item_raw is not None
            else coerce_thread_item({"type": "unknown"})
        )
        return ItemStartedEvent(item=item)
    if event_type == "item.updated":
        item_raw = raw.get("item")
        item = (
            coerce_thread_item(cast(Union[ThreadItem, Mapping[str, Any]], item_raw))
            if item_raw is not None
            else coerce_thread_item({"type": "unknown"})
        )
        return ItemUpdatedEvent(item=item)
    if event_type == "item.completed":
        item_raw = raw.get("item")
        item = (
            coerce_thread_item(cast(Union[ThreadItem, Mapping[str, Any]], item_raw))
            if item_raw is not None
            else coerce_thread_item({"type": "unknown"})
        )
        return ItemCompletedEvent(item=item)
    if event_type == "error":
        return ThreadErrorEvent(message=cast(str, raw.get("message", "")))

    return _UnknownThreadEvent(
        type=cast(str, event_type) if event_type is not None else "unknown",
        payload=dict(raw),
    )
