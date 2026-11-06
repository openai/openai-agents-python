"""Admission and persistence for completed Classic Session items."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..classic.exceptions import UserError
from ..classic.items import ItemHelpers, TResponseInputItem
from ..classic.memory.openai_conversations_session import OpenAIConversationsSession
from ..classic.memory.openai_responses_compaction_session import _ALL_SESSION_ITEMS_LIMIT
from ..classic.memory.session import Session, await_session_mutation
from ..classic.memory.session_settings import resolve_session_limit
from ..classic.run_context import RunContextWrapper
from ..classic.run_internal.items import drop_orphan_function_calls, normalize_input_items_for_api
from ..classic.run_internal.session_persistence import (
    _is_unpersistable_for_openai_conversation,
    _sanitize_openai_conversation_history_item_for_model_input,
    _sanitize_openai_conversation_item,
    _session_add_items,
    _session_get_items,
)

_INPUT_PARTS: dict[str, str | None] = {
    "input_text": "text",
    "input_image": None,
    "input_file": None,
}
_MESSAGE_PARTS = {
    **_INPUT_PARTS,
    # Classic Chat Completions also accepts these content parts.
    "input_audio": None,
    "image_url": None,
    "text": "text",
    "video_url": None,
}
_ASSISTANT_PARTS = {**_MESSAGE_PARTS, "output_text": "text", "refusal": "refusal"}


def _unsettled_history() -> UserError:
    return UserError(
        "SDKRunner requires completed Classic Session messages and complete function call pairs. "
        "Pending or unknown effects require MIG.8. Resolve them in the owning runtime before "
        "importing the completed Session."
    )


@dataclass(frozen=True)
class _CompletedHistory:
    items: list[TResponseInputItem]
    call_ids: set[str]


def _valid_content_parts(content: object, parts: dict[str, str | None]) -> bool:
    if not isinstance(content, list):
        return False
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if not isinstance(kind, str) or kind not in parts:
            return False
        field = parts[kind]
        if field is not None and not isinstance(part.get(field), str):
            return False
    return True


def _capture_completed_history(items: list[TResponseInputItem]) -> _CompletedHistory:
    if not isinstance(items, list):
        raise _unsettled_history()
    normalized = normalize_input_items_for_api(items)
    captured = deepcopy(ItemHelpers.input_to_new_input_list(normalized))
    call_ids: set[str] = set()
    pending: set[str] = set()

    for item in captured:
        if not isinstance(item, dict) or item.get("status") not in (None, "completed"):
            raise _unsettled_history()
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant", "developer", "system") or (
                pending and role != "assistant"
            ):
                raise _unsettled_history()
            content = item.get("content")
            if not isinstance(content, str) and not _valid_content_parts(
                content, _ASSISTANT_PARTS if role == "assistant" else _MESSAGE_PARTS
            ):
                raise _unsettled_history()
            continue
        if kind == "reasoning":
            content = item.get("content")
            encrypted_content = item.get("encrypted_content")
            item_id = item.get("id")
            if (
                not _valid_content_parts(item.get("summary"), {"summary_text": "text"})
                or (
                    content is not None
                    and not _valid_content_parts(content, {"reasoning_text": "text"})
                )
                or (encrypted_content is not None and not isinstance(encrypted_content, str))
                # Classic can persist reasoning with its ID omitted.
                or (item_id is not None and not isinstance(item_id, str))
            ):
                raise _unsettled_history()
            continue
        if kind == "compaction":
            raise UserError(
                "SDKRunner cannot import opaque compaction records from Classic Session history. "
                "Continue this conversation with agents.classic.Runner."
            )
        if kind not in ("function_call", "function_call_output"):
            raise _unsettled_history()

        call_id = item.get("call_id")
        caller = item.get("caller")
        if (
            not isinstance(call_id, str)
            or not call_id
            or item.get("async")
            or caller not in (None, {"type": "direct"})
        ):
            raise _unsettled_history()
        if kind == "function_call":
            name = item.get("name")
            if (
                call_id in call_ids
                or not isinstance(name, str)
                or not name
                or not isinstance(item.get("arguments"), str)
            ):
                raise _unsettled_history()
            call_ids.add(call_id)
            pending.add(call_id)
        else:
            output = item.get("output")
            if call_id not in pending or (
                not isinstance(output, str) and not _valid_content_parts(output, _INPUT_PARTS)
            ):
                raise _unsettled_history()
            pending.remove(call_id)

    if pending:
        raise _unsettled_history()
    return _CompletedHistory(captured, call_ids)


async def _load_completed_history(
    session: Session, wrapper: RunContextWrapper[Any]
) -> _CompletedHistory:
    limit = resolve_session_limit(None, getattr(session, "session_settings", None))
    if limit is None:
        read = _session_get_items(session, wrapper=wrapper, capture_compaction_generation=True)
    else:
        read = _session_get_items(
            session,
            limit=_ALL_SESSION_ITEMS_LIMIT,
            wrapper=wrapper,
            capture_compaction_generation=True,
        )
    read_task = asyncio.create_task(read)
    try:
        # Keep cancellation with the invocation, even if the store suppresses it.
        await asyncio.wait({read_task})
    except asyncio.CancelledError:
        read_task.cancel()
        with suppress(asyncio.CancelledError):
            await await_session_mutation(asyncio.gather(read_task, return_exceptions=True))
        raise
    items = read_task.result()
    if limit is not None and isinstance(items, list) and len(items) >= _ALL_SESSION_ITEMS_LIMIT:
        raise UserError(
            "SDKRunner could not verify the complete Classic Session history because the "
            "snapshot reached the read limit. Continue this Session with agents.classic.Runner."
        )

    # Validate the snapshot and retain every historical call ID before applying the Model cutoff.
    history = _capture_completed_history(items)
    visible = history.items
    if limit is not None:
        visible = visible[-limit:] if limit > 0 else []
        visible = drop_orphan_function_calls(
            visible, output_pruning_indexes=set(range(len(visible)))
        )
    if isinstance(session, OpenAIConversationsSession):
        visible = [
            _sanitize_openai_conversation_history_item_for_model_input(item) for item in visible
        ]
    return _CompletedHistory(visible, history.call_ids)


async def _append_completed_history(
    session: Session, items: list[TResponseInputItem], wrapper: RunContextWrapper[Any]
) -> None:
    delta = _capture_completed_history(items).items
    if isinstance(session, OpenAIConversationsSession):
        delta = [_sanitize_openai_conversation_item(item) for item in delta]
        delta = [item for item in delta if not _is_unpersistable_for_openai_conversation(item)]
    if delta:
        await await_session_mutation(_session_add_items(session, delta, wrapper=wrapper))
