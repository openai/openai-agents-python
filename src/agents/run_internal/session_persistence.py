"""
Session persistence helpers for the run pipeline. Only internal persistence/retry helpers
live here; public session interfaces stay in higher-level modules.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import Sequence
from typing import Any, cast

from ..exceptions import UserError
from ..items import ItemHelpers, RunItem, TResponseInputItem
from ..logger import logger
from ..memory import (
    Session,
    SessionInputCallback,
    is_openai_responses_compaction_aware_session,
)
from ..memory.openai_conversations_session import OpenAIConversationsSession
from ..run_state import RunState
from .items import (
    deduplicate_input_items,
    drop_orphan_function_calls,
    ensure_input_item_format,
    fingerprint_input_item,
    normalize_input_items_for_api,
)
from .oai_conversation import OpenAIServerConversationTracker

__all__ = [
    "prepare_input_with_session",
    "persist_session_items_for_guardrail_trip",
    "save_result_to_session",
    "rewind_session_items",
    "wait_for_session_cleanup",
]


async def prepare_input_with_session(
    input: str | list[TResponseInputItem],
    session: Session | None,
    session_input_callback: SessionInputCallback | None,
    *,
    include_history_in_prepared_input: bool = True,
    preserve_dropped_new_items: bool = False,
) -> tuple[str | list[TResponseInputItem], list[TResponseInputItem]]:
    """
    Prepare input by combining it with session history and applying the optional input callback.
    Returns the prepared input plus the appended items that should be persisted separately.
    """

    if session is None:
        return input, []

    if (
        include_history_in_prepared_input
        and session_input_callback is None
        and isinstance(input, list)
    ):
        raise UserError(
            "list inputs require a `RunConfig.session_input_callback` "
            "to manage the history manually."
        )

    history = await session.get_items()
    converted_history = [ensure_input_item_format(item) for item in history]

    new_input_list = [
        ensure_input_item_format(item) for item in ItemHelpers.input_to_new_input_list(input)
    ]

    if session_input_callback is None or not include_history_in_prepared_input:
        prepared_items_raw: list[TResponseInputItem] = (
            converted_history + new_input_list
            if include_history_in_prepared_input
            else list(new_input_list)
        )
        appended_items = list(new_input_list)
    else:
        history_for_callback = copy.deepcopy(converted_history)
        new_items_for_callback = copy.deepcopy(new_input_list)
        combined = session_input_callback(history_for_callback, new_items_for_callback)
        if inspect.isawaitable(combined):
            combined = await combined
        if not isinstance(combined, list):
            raise UserError("Session input callback must return a list of input items.")

        history_refs = _build_reference_map(history_for_callback)
        new_refs = _build_reference_map(new_items_for_callback)
        history_counts = _build_frequency_map(history_for_callback)
        new_counts = _build_frequency_map(new_items_for_callback)

        appended: list[Any] = []
        for item in combined:
            key = _session_item_key(item)
            if _consume_reference(new_refs, key, item):
                new_counts[key] = max(new_counts.get(key, 0) - 1, 0)
                appended.append(item)
                continue
            if _consume_reference(history_refs, key, item):
                history_counts[key] = max(history_counts.get(key, 0) - 1, 0)
                continue
            if history_counts.get(key, 0) > 0:
                history_counts[key] = history_counts.get(key, 0) - 1
                continue
            if new_counts.get(key, 0) > 0:
                new_counts[key] = max(new_counts.get(key, 0) - 1, 0)
                appended.append(item)
                continue
            appended.append(item)

        appended_items = [ensure_input_item_format(item) for item in appended]

        if include_history_in_prepared_input:
            prepared_items_raw = combined
        elif appended_items:
            prepared_items_raw = appended_items
        else:
            prepared_items_raw = new_items_for_callback if preserve_dropped_new_items else []

    prepared_as_inputs = [ensure_input_item_format(item) for item in prepared_items_raw]
    filtered = drop_orphan_function_calls(prepared_as_inputs)
    normalized = normalize_input_items_for_api(filtered)
    deduplicated = deduplicate_input_items(normalized)

    return deduplicated, [ensure_input_item_format(item) for item in appended_items]


async def persist_session_items_for_guardrail_trip(
    session: Session | None,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    session_input_items_for_persistence: list[TResponseInputItem] | None,
    original_user_input: str | list[TResponseInputItem] | None,
    run_state: RunState | None,
) -> list[TResponseInputItem] | None:
    """
    Persist input items when a guardrail tripwire is triggered.
    """
    if session is None or server_conversation_tracker is not None:
        return session_input_items_for_persistence

    updated_session_input_items = session_input_items_for_persistence
    if updated_session_input_items is None and original_user_input is not None:
        updated_session_input_items = ItemHelpers.input_to_new_input_list(original_user_input)

    input_items_for_save: list[TResponseInputItem] = (
        updated_session_input_items if updated_session_input_items is not None else []
    )
    await save_result_to_session(session, input_items_for_save, [], run_state)
    return updated_session_input_items


async def save_result_to_session(
    session: Session | None,
    original_input: str | list[TResponseInputItem],
    new_items: list[RunItem],
    run_state: RunState | None = None,
    *,
    response_id: str | None = None,
) -> None:
    """
    Persist a turn to the session store, keeping track of what was already saved so retries
    during streaming do not duplicate tool outputs or inputs.
    """
    already_persisted = run_state._current_turn_persisted_item_count if run_state else 0

    if session is None:
        return

    new_run_items: list[RunItem]
    if already_persisted >= len(new_items):
        new_run_items = []
    else:
        new_run_items = new_items[already_persisted:]
    if run_state and new_items and new_run_items:
        missing_outputs = [
            item
            for item in new_items
            if item.type == "tool_call_output_item" and item not in new_run_items
        ]
        if missing_outputs:
            new_run_items = missing_outputs + new_run_items

    input_list: list[TResponseInputItem] = []
    if original_input:
        input_list = [
            ensure_input_item_format(item)
            for item in ItemHelpers.input_to_new_input_list(original_input)
        ]

    items_to_convert = [item for item in new_run_items if item.type != "tool_approval_item"]

    new_items_as_input: list[TResponseInputItem] = [
        ensure_input_item_format(item.to_input_item()) for item in items_to_convert
    ]

    ignore_ids_for_matching = isinstance(session, OpenAIConversationsSession) or getattr(
        session, "_ignore_ids_for_matching", False
    )
    serialized_new_items = [
        fingerprint_input_item(item, ignore_ids_for_matching=ignore_ids_for_matching) or repr(item)
        for item in new_items_as_input
    ]

    items_to_save = deduplicate_input_items(input_list + new_items_as_input)

    if isinstance(session, OpenAIConversationsSession) and items_to_save:
        sanitized: list[TResponseInputItem] = []
        for item in items_to_save:
            if isinstance(item, dict):
                clean_item = dict(item)
                clean_item.pop("id", None)
                clean_item.pop("provider_data", None)
                sanitized.append(cast(TResponseInputItem, clean_item))
            else:
                sanitized.append(item)
        items_to_save = sanitized

    serialized_to_save: list[str] = [
        fingerprint_input_item(item, ignore_ids_for_matching=ignore_ids_for_matching) or repr(item)
        for item in items_to_save
    ]
    serialized_to_save_counts: dict[str, int] = {}
    for serialized in serialized_to_save:
        serialized_to_save_counts[serialized] = serialized_to_save_counts.get(serialized, 0) + 1

    saved_run_items_count = 0
    for serialized in serialized_new_items:
        if serialized_to_save_counts.get(serialized, 0) > 0:
            serialized_to_save_counts[serialized] -= 1
            saved_run_items_count += 1

    if len(items_to_save) == 0:
        if run_state:
            run_state._current_turn_persisted_item_count = already_persisted + saved_run_items_count
        return

    await session.add_items(items_to_save)

    if run_state:
        run_state._current_turn_persisted_item_count = already_persisted + saved_run_items_count

    if response_id and is_openai_responses_compaction_aware_session(session):
        await session.run_compaction({"response_id": response_id})


async def rewind_session_items(
    session: Session | None,
    items: Sequence[TResponseInputItem],
    server_tracker: OpenAIServerConversationTracker | None = None,
) -> None:
    """
    Best-effort helper to roll back items recently persisted to a session when a conversation
    retry is needed, so we do not accumulate duplicate inputs on lock errors.
    """
    if session is None or not items:
        return

    pop_item = getattr(session, "pop_item", None)
    if not callable(pop_item):
        return

    ignore_ids_for_matching = isinstance(session, OpenAIConversationsSession) or getattr(
        session, "_ignore_ids_for_matching", False
    )
    target_serializations: list[str] = []
    for item in items:
        serialized = fingerprint_input_item(item, ignore_ids_for_matching=ignore_ids_for_matching)
        if serialized:
            target_serializations.append(serialized)

    if not target_serializations:
        return

    logger.debug(
        "Rewinding session items due to conversation retry (targets=%d)",
        len(target_serializations),
    )

    for i, target in enumerate(target_serializations):
        logger.debug("Rewind target %d (first 300 chars): %s", i, target[:300])

    snapshot_serializations = target_serializations.copy()

    remaining = target_serializations.copy()

    while remaining:
        try:
            result = pop_item()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.warning("Failed to rewind session item: %s", exc)
            break
        else:
            if result is None:
                break

            popped_serialized = fingerprint_input_item(
                result, ignore_ids_for_matching=ignore_ids_for_matching
            )

            logger.debug("Popped item type during rewind: %s", type(result).__name__)
            if popped_serialized:
                logger.debug("Popped serialized (first 300 chars): %s", popped_serialized[:300])
            else:
                logger.debug("Popped serialized: None")

            logger.debug("Number of remaining targets: %d", len(remaining))
            if remaining and popped_serialized:
                logger.debug("First target (first 300 chars): %s", remaining[0][:300])
                logger.debug("Match found: %s", popped_serialized in remaining)
                if len(remaining) > 0:
                    first_target = remaining[0]
                    if abs(len(first_target) - len(popped_serialized)) < 50:
                        logger.debug(
                            "Length comparison - popped: %d, target: %d",
                            len(popped_serialized),
                            len(first_target),
                        )

            if popped_serialized and popped_serialized in remaining:
                remaining.remove(popped_serialized)

    if remaining:
        logger.warning(
            "Unable to fully rewind session; %d items still unmatched after retry",
            len(remaining),
        )
    else:
        await wait_for_session_cleanup(
            session,
            snapshot_serializations,
            ignore_ids_for_matching=ignore_ids_for_matching,
        )

    if session is None or server_tracker is None:
        return

    try:
        latest_items = await session.get_items(limit=1)
    except Exception as exc:
        logger.debug("Failed to peek session items while rewinding: %s", exc)
        return

    if not latest_items:
        return

    latest_id = latest_items[0].get("id")
    if isinstance(latest_id, str) and latest_id in server_tracker.server_item_ids:
        return

    logger.debug("Stripping stray conversation items until we reach a known server item")
    while True:
        try:
            result = pop_item()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.warning("Failed to strip stray session item: %s", exc)
            break

        if result is None:
            break

        stripped_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        if isinstance(stripped_id, str) and stripped_id in server_tracker.server_item_ids:
            break


async def wait_for_session_cleanup(
    session: Session | None,
    serialized_targets: Sequence[str],
    *,
    max_attempts: int = 5,
    ignore_ids_for_matching: bool = False,
) -> None:
    """
    Confirm that rewound items are no longer present in the session tail so the store stays
    consistent before the next retry attempt begins.
    """
    if session is None or not serialized_targets:
        return

    window = len(serialized_targets) + 2

    for attempt in range(max_attempts):
        try:
            tail_items = await session.get_items(limit=window)
        except Exception as exc:
            logger.debug("Failed to verify session cleanup (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(0.1 * (attempt + 1))
            continue

        serialized_tail: set[str] = set()
        for item in tail_items:
            serialized = fingerprint_input_item(
                item, ignore_ids_for_matching=ignore_ids_for_matching
            )
            if serialized:
                serialized_tail.add(serialized)

        if not any(serial in serialized_tail for serial in serialized_targets):
            return

        await asyncio.sleep(0.1 * (attempt + 1))

    logger.debug(
        "Session cleanup verification exhausted attempts; targets may still linger temporarily"
    )


# --------------------------
# Private helpers
# --------------------------


def _session_item_key(item: Any) -> str:
    """Return a stable representation of a session item for comparison."""
    try:
        if hasattr(item, "model_dump"):
            payload = item.model_dump(exclude_unset=True)
        elif isinstance(item, dict):
            payload = item
        else:
            payload = ensure_input_item_format(item)
        return json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        return repr(item)


def _build_reference_map(items: Sequence[Any]) -> dict[str, list[Any]]:
    """Map serialized keys to the concrete session items used to build them."""
    refs: dict[str, list[Any]] = {}
    for item in items:
        key = _session_item_key(item)
        refs.setdefault(key, []).append(item)
    return refs


def _consume_reference(ref_map: dict[str, list[Any]], key: str, candidate: Any) -> bool:
    """Remove a specific candidate from a reference map when it is consumed."""
    candidates = ref_map.get(key)
    if not candidates:
        return False
    for idx, existing in enumerate(candidates):
        if existing is candidate:
            candidates.pop(idx)
            if not candidates:
                ref_map.pop(key, None)
            return True
    return False


def _build_frequency_map(items: Sequence[Any]) -> dict[str, int]:
    """Count how many times each serialized key appears in a collection."""
    freq: dict[str, int] = {}
    for item in items:
        key = _session_item_key(item)
        freq[key] = freq.get(key, 0) + 1
    return freq
