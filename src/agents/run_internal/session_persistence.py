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
from ..items import HandoffOutputItem, ItemHelpers, RunItem, ToolCallOutputItem, TResponseInputItem
from ..logger import logger
from ..memory import (
    OpenAIResponsesCompactionArgs,
    Session,
    SessionInputCallback,
    SessionSettings,
    is_openai_responses_compaction_aware_session,
)
from ..memory.openai_conversations_session import OpenAIConversationsSession
from ..run_state import RunState
from .items import (
    ReasoningItemIdPolicy,
    copy_input_items,
    deduplicate_input_items_preferring_latest,
    drop_orphan_function_calls,
    ensure_input_item_format,
    fingerprint_input_item,
    normalize_input_items_for_api,
    run_item_to_input_item,
    strip_internal_input_item_metadata,
)
from .oai_conversation import OpenAIServerConversationTracker
from .run_steps import SingleStepResult

__all__ = [
    "prepare_input_with_session",
    "persist_session_items_for_guardrail_trip",
    "session_items_for_turn",
    "resumed_turn_items",
    "save_result_to_session",
    "save_resumed_turn_items",
    "update_run_state_after_resume",
    "rewind_session_items",
    "wait_for_session_cleanup",
]


async def prepare_input_with_session(
    input: str | list[TResponseInputItem],
    session: Session | None,
    session_input_callback: SessionInputCallback | None,
    session_settings: SessionSettings | None = None,
    *,
    include_history_in_prepared_input: bool = True,
    preserve_dropped_new_items: bool = False,
) -> tuple[str | list[TResponseInputItem], list[TResponseInputItem]]:
    """Prepare model input from session history plus the new turn input.

    Returns a tuple of:

    1. The prepared input that should be sent to the model after normalization and dedupe.
    2. The subset of items that should be appended to the session store for this turn.

    The second value is intentionally not "everything returned by the callback". When a
    ``session_input_callback`` reorders or filters history, we still need to persist only the
    items that belong to the new turn. This function therefore compares the callback output
    against deep-copied history and new-input lists, first by object identity and then by
    content frequency, so retries and custom merge strategies do not accidentally re-persist
    old history as fresh input.
    """

    if session is None:
        return input, []

    resolved_settings = getattr(session, "session_settings", None) or SessionSettings()
    if session_settings is not None:
        resolved_settings = resolved_settings.resolve(session_settings)

    if resolved_settings.limit is not None:
        history = await session.get_items(limit=resolved_settings.limit)
    else:
        history = await session.get_items()
    is_openai_conversation_session = isinstance(session, OpenAIConversationsSession)
    converted_history = [
        strip_internal_input_item_metadata(ensure_input_item_format(item)) for item in history
    ]

    new_input_list = [
        ensure_input_item_format(item) for item in ItemHelpers.input_to_new_input_list(input)
    ]

    prune_history_indexes: set[int] = set()

    if session_input_callback is None or not include_history_in_prepared_input:
        prepared_items_raw: list[TResponseInputItem] = (
            converted_history + new_input_list
            if include_history_in_prepared_input
            else list(new_input_list)
        )
        appended_items = list(new_input_list)
        if include_history_in_prepared_input:
            prune_history_indexes = set(range(len(converted_history)))
    else:
        if not callable(session_input_callback):
            raise UserError(
                f"Invalid `session_input_callback` value: {session_input_callback}. "
                "Choose between `None` or a custom callable function."
            )
        history_for_callback = copy.deepcopy(converted_history)
        new_items_for_callback = copy.deepcopy(new_input_list)
        combined = session_input_callback(history_for_callback, new_items_for_callback)
        if inspect.isawaitable(combined):
            combined = await combined
        if not isinstance(combined, list):
            raise UserError("Session input callback must return a list of input items.")

        # The callback may reorder, drop, or duplicate items. Keep separate reference maps for
        # the copied history and copied new-input lists so we can reconstruct which output items
        # belong to the new turn and therefore still need to be persisted.
        history_refs = _build_reference_map(
            history_for_callback,
            ignore_openai_conversation_item_ids=is_openai_conversation_session,
        )
        new_refs = _build_reference_map(new_items_for_callback)
        history_counts = _build_frequency_map(
            history_for_callback,
            ignore_openai_conversation_item_ids=is_openai_conversation_session,
        )
        new_counts = _build_frequency_map(new_items_for_callback)

        appended: list[Any] = []
        for combined_index, item in enumerate(combined):
            history_key = _session_item_key(
                item,
                ignore_openai_conversation_item_ids=is_openai_conversation_session,
            )
            new_key = _session_item_key(item)
            if _consume_reference(new_refs, new_key, item):
                new_counts[new_key] = max(new_counts.get(new_key, 0) - 1, 0)
                appended.append(item)
                continue
            if _consume_reference(history_refs, history_key, item):
                history_counts[history_key] = max(history_counts.get(history_key, 0) - 1, 0)
                prune_history_indexes.add(combined_index)
                continue
            if history_counts.get(history_key, 0) > 0:
                history_counts[history_key] = history_counts.get(history_key, 0) - 1
                prune_history_indexes.add(combined_index)
                continue
            if new_counts.get(new_key, 0) > 0:
                new_counts[new_key] = max(new_counts.get(new_key, 0) - 1, 0)
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

    # Normalize exactly as the runtime does elsewhere so the prepared model input and the
    # persisted session items are derived from the same item shape and dedupe rules.
    if is_openai_conversation_session and prune_history_indexes:
        prepared_items_raw = _sanitize_openai_conversation_history_items_for_model_input(
            prepared_items_raw,
            prune_history_indexes,
        )
    prepared_as_inputs = [ensure_input_item_format(item) for item in prepared_items_raw]
    filtered = drop_orphan_function_calls(
        prepared_as_inputs,
        pruning_indexes=prune_history_indexes,
    )
    normalized = normalize_input_items_for_api(filtered)
    deduplicated = deduplicate_input_items_preferring_latest(normalized)

    appended_as_inputs = [ensure_input_item_format(item) for item in appended_items]
    return deduplicated, normalize_input_items_for_api(appended_as_inputs)


async def persist_session_items_for_guardrail_trip(
    session: Session | None,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    session_input_items_for_persistence: list[TResponseInputItem] | None,
    original_user_input: str | list[TResponseInputItem] | None,
    run_state: RunState | None,
    store: bool | None = None,
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
    await save_result_to_session(session, input_items_for_save, [], run_state, store=store)
    return updated_session_input_items


def session_items_for_turn(turn_result: SingleStepResult) -> list[RunItem]:
    """Return the items to persist for a turn, preferring session_step_items when set."""
    items = (
        turn_result.session_step_items
        if turn_result.session_step_items is not None
        else turn_result.new_step_items
    )
    return list(items)


def resumed_turn_items(turn_result: SingleStepResult) -> tuple[list[RunItem], list[RunItem]]:
    """Return generated and session items for a resumed turn."""
    generated_items = list(turn_result.pre_step_items) + list(turn_result.new_step_items)
    turn_session_items = session_items_for_turn(turn_result)
    return generated_items, turn_session_items


def update_run_state_after_resume(
    run_state: RunState,
    *,
    turn_result: SingleStepResult,
    generated_items: list[RunItem],
    session_items: list[RunItem] | None = None,
) -> None:
    """Update run state fields after resolving an interruption."""
    run_state._original_input = copy_input_items(turn_result.original_input)
    run_state._generated_items = generated_items
    if session_items is not None:
        run_state._session_items = list(session_items)
    run_state._current_step = turn_result.next_step  # type: ignore[assignment]


async def save_result_to_session(
    session: Session | None,
    original_input: str | list[TResponseInputItem],
    new_items: list[RunItem],
    run_state: RunState | None = None,
    *,
    response_id: str | None = None,
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
    store: bool | None = None,
) -> int:
    """
    Persist a turn to the session store, keeping track of what was already saved so retries
    during streaming do not duplicate tool outputs or inputs.

    Returns:
        The number of new run items persisted for this call.
    """
    already_persisted = run_state._current_turn_persisted_item_count if run_state else 0

    if session is None:
        return 0

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
        input_list = normalize_input_items_for_api(
            [
                ensure_input_item_format(item)
                for item in ItemHelpers.input_to_new_input_list(original_input)
            ]
        )

    resolved_reasoning_item_id_policy = (
        reasoning_item_id_policy
        if reasoning_item_id_policy is not None
        else (run_state._reasoning_item_id_policy if run_state is not None else None)
    )
    new_items_as_input: list[TResponseInputItem] = []
    for run_item in new_run_items:
        converted = run_item_to_input_item(run_item, resolved_reasoning_item_id_policy)
        if converted is None:
            continue
        new_items_as_input.append(ensure_input_item_format(converted))

    is_openai_conversation_session = isinstance(session, OpenAIConversationsSession)
    ignore_ids_for_matching = _ignore_ids_for_matching(session)

    new_items_for_fingerprint = (
        [_sanitize_openai_conversation_item(item) for item in new_items_as_input]
        if is_openai_conversation_session
        else new_items_as_input
    )
    serialized_new_items = [
        _fingerprint_or_repr(item, ignore_ids_for_matching=ignore_ids_for_matching)
        for item in new_items_for_fingerprint
    ]

    items_to_save = deduplicate_input_items_preferring_latest(input_list + new_items_as_input)

    if is_openai_conversation_session and items_to_save:
        items_to_save = [_sanitize_openai_conversation_item(item) for item in items_to_save]

    serialized_to_save: list[str] = [
        _fingerprint_or_repr(item, ignore_ids_for_matching=ignore_ids_for_matching)
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
        return saved_run_items_count

    await session.add_items(items_to_save)

    if run_state:
        run_state._current_turn_persisted_item_count = already_persisted + saved_run_items_count

    if response_id and is_openai_responses_compaction_aware_session(session):
        has_local_tool_outputs = any(
            isinstance(item, ToolCallOutputItem | HandoffOutputItem) for item in new_items
        )
        if has_local_tool_outputs:
            defer_compaction = getattr(session, "_defer_compaction", None)
            if callable(defer_compaction):
                result = defer_compaction(response_id, store=store)
                if inspect.isawaitable(result):
                    await result
            logger.debug(
                "skip: deferring compaction for response %s due to local tool outputs",
                response_id,
            )
            return saved_run_items_count

        deferred_response_id = None
        get_deferred = getattr(session, "_get_deferred_compaction_response_id", None)
        if callable(get_deferred):
            deferred_response_id = get_deferred()
        force_compaction = deferred_response_id is not None
        if force_compaction:
            logger.debug(
                "compact: forcing for response %s after deferred %s",
                response_id,
                deferred_response_id,
            )
        compaction_args: OpenAIResponsesCompactionArgs = {
            "response_id": response_id,
            "force": force_compaction,
        }
        if store is not None:
            compaction_args["store"] = store
        await session.run_compaction(compaction_args)

    return saved_run_items_count


async def save_resumed_turn_items(
    *,
    session: Session | None,
    items: list[RunItem],
    persisted_count: int,
    response_id: str | None,
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
    store: bool | None = None,
) -> int:
    """Persist resumed turn items and return the updated persisted count."""
    if session is None or not items:
        return persisted_count
    saved_count = await save_result_to_session(
        session,
        [],
        list(items),
        None,
        response_id=response_id,
        reasoning_item_id_policy=reasoning_item_id_policy,
        store=store,
    )
    return persisted_count + saved_count


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

    ignore_ids_for_matching = _ignore_ids_for_matching(session)
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
    rewound = await _rewind_session_tail_suffix(
        session=session,
        pop_item=pop_item,
        expected_serializations=target_serializations,
        ignore_ids_for_matching=ignore_ids_for_matching,
        mismatch_warning=(
            "Skipping session rewind because the current tail does not match the retry-owned suffix"
        ),
        pop_failure_warning="Failed to rewind session item: %s",
    )
    if not rewound:
        return

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

    try:
        session_items = await session.get_items()
    except Exception as exc:
        logger.debug("Failed to inspect session tail while stripping stray items: %s", exc)
        return

    stray_serializations = _collect_retry_owned_tail_serializations(
        session_items,
        server_tracker=server_tracker,
        ignore_ids_for_matching=ignore_ids_for_matching,
    )
    if not stray_serializations:
        return

    logger.debug(
        "Stripping %d retry-owned conversation items until the session tail reaches "
        "a known server item",
        len(stray_serializations),
    )
    await _rewind_session_tail_suffix(
        session=session,
        pop_item=pop_item,
        expected_serializations=stray_serializations,
        ignore_ids_for_matching=ignore_ids_for_matching,
        mismatch_warning=(
            "Skipping stray session cleanup because the current tail no longer matches "
            "retry-owned conversation items"
        ),
        pop_failure_warning="Failed to strip stray session item: %s",
    )


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


def _ignore_ids_for_matching(session: Session) -> bool:
    """Return whether session fingerprinting should ignore item IDs."""
    return isinstance(session, OpenAIConversationsSession) or getattr(
        session, "_ignore_ids_for_matching", False
    )


_OPENAI_CONVERSATION_ITEM_TYPES_WITH_REQUIRED_ID: frozenset[str] = frozenset(
    {
        "file_search_call",
        "web_search_call",
        "computer_call",
        "code_interpreter_call",
        "image_generation_call",
        "local_shell_call",
        "local_shell_call_output",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_call",
        "item_reference",
    }
)


def _sanitize_openai_conversation_item(item: TResponseInputItem) -> TResponseInputItem:
    """Remove provider-specific fields before fingerprinting or persistence.

    Some Responses input item types require their server-assigned ``id`` when they are
    persisted through the Conversations API. Other item IDs remain stripped so replayed
    messages, reasoning items, and function calls do not carry stale provider IDs.
    """
    if isinstance(item, dict):
        clean_item = cast(dict[str, Any], strip_internal_input_item_metadata(item))
        if not _openai_conversation_item_requires_id(clean_item):
            clean_item.pop("id", None)
        clean_item.pop("provider_data", None)
        return cast(TResponseInputItem, clean_item)
    return item


def _openai_conversation_item_requires_id(item: dict[str, Any]) -> bool:
    """Return whether the Conversations create-item schema requires this item's top-level ID."""
    return item.get("type") in _OPENAI_CONVERSATION_ITEM_TYPES_WITH_REQUIRED_ID


def _sanitize_openai_conversation_history_items_for_model_input(
    items: Sequence[TResponseInputItem],
    history_indexes: set[int],
) -> list[TResponseInputItem]:
    """Remove Conversation item metadata only from session-history items sent to the model."""
    sanitized_items: list[TResponseInputItem] = []
    for index, item in enumerate(items):
        if index in history_indexes:
            sanitized_items.append(_sanitize_openai_conversation_history_item_for_model_input(item))
        else:
            sanitized_items.append(item)
    return sanitized_items


def _sanitize_openai_conversation_history_item_for_model_input(
    item: TResponseInputItem,
) -> TResponseInputItem:
    """Remove Conversation replay metadata from assistant messages only."""
    if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant":
        clean_item = cast(dict[str, Any], strip_internal_input_item_metadata(item))
        clean_item.pop("id", None)
        clean_item.pop("provider_data", None)
        return cast(TResponseInputItem, clean_item)
    return item


def _fingerprint_or_repr(item: TResponseInputItem, *, ignore_ids_for_matching: bool) -> str:
    """Fingerprint an item or fall back to repr when unavailable."""
    return fingerprint_input_item(item, ignore_ids_for_matching=ignore_ids_for_matching) or repr(
        item
    )


async def _rewind_session_tail_suffix(
    *,
    session: Session,
    pop_item: Any,
    expected_serializations: Sequence[str],
    ignore_ids_for_matching: bool,
    mismatch_warning: str,
    pop_failure_warning: str,
) -> bool:
    """Remove an exact serialized suffix from the session tail, aborting when the tail diverges."""
    if not expected_serializations:
        return True

    try:
        tail_items = await session.get_items(limit=len(expected_serializations))
    except Exception as exc:
        logger.warning(pop_failure_warning, exc)
        return False

    if len(tail_items) != len(expected_serializations):
        logger.warning(mismatch_warning)
        return False

    tail_serializations: list[str] = []
    for item in tail_items:
        serialized = fingerprint_input_item(item, ignore_ids_for_matching=ignore_ids_for_matching)
        if not serialized:
            logger.warning(mismatch_warning)
            return False
        tail_serializations.append(serialized)

    if tail_serializations != list(expected_serializations):
        logger.warning(mismatch_warning)
        return False

    popped_items: list[TResponseInputItem] = []
    for expected in reversed(expected_serializations):
        try:
            result = pop_item()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await _restore_popped_session_items(session, popped_items)
            logger.warning(pop_failure_warning, exc)
            return False

        if result is None:
            await _restore_popped_session_items(session, popped_items)
            logger.warning(mismatch_warning)
            return False

        popped_items.append(result)
        popped_serialized = fingerprint_input_item(
            result, ignore_ids_for_matching=ignore_ids_for_matching
        )
        if popped_serialized != expected:
            await _restore_popped_session_items(session, popped_items)
            logger.warning(mismatch_warning)
            return False

    return True


async def _restore_popped_session_items(
    session: Session, popped_items: Sequence[TResponseInputItem]
) -> None:
    """Best-effort restoration for items popped during a failed rewind attempt."""
    if not popped_items:
        return

    add_items = getattr(session, "add_items", None)
    if not callable(add_items):
        return

    try:
        result = add_items(list(reversed(popped_items)))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.warning("Failed to restore session items after a rewind mismatch: %s", exc)


def _collect_retry_owned_tail_serializations(
    session_items: Sequence[TResponseInputItem],
    *,
    server_tracker: OpenAIServerConversationTracker,
    ignore_ids_for_matching: bool,
) -> list[str]:
    """Return the contiguous retry-owned tail suffix that can be safely stripped."""
    stray_tail: list[str] = []

    for item in reversed(session_items):
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if isinstance(item_id, str) and item_id in server_tracker.server_item_ids:
            return list(reversed(stray_tail))

        serialized = fingerprint_input_item(item, ignore_ids_for_matching=ignore_ids_for_matching)
        if serialized and serialized in server_tracker.sent_item_fingerprints:
            stray_tail.append(serialized)
            continue

        logger.warning(
            "Skipping stray session cleanup because the current tail contains items unrelated "
            "to this retry"
        )
        return []

    if stray_tail:
        logger.warning(
            "Skipping stray session cleanup because no known server item was found before the "
            "session boundary"
        )
    return []


def _session_item_key(item: Any, *, ignore_openai_conversation_item_ids: bool = False) -> str:
    """Return a stable representation of a session item for comparison."""
    try:
        if hasattr(item, "model_dump"):
            payload = item.model_dump(exclude_unset=True)
        elif isinstance(item, dict):
            payload = item
        else:
            payload = ensure_input_item_format(item)
        if isinstance(payload, dict):
            payload = cast(
                dict[str, Any],
                strip_internal_input_item_metadata(cast(TResponseInputItem, payload)),
            )
            if ignore_openai_conversation_item_ids:
                payload = cast(
                    dict[str, Any],
                    _sanitize_openai_conversation_history_item_for_model_input(
                        cast(TResponseInputItem, payload)
                    ),
                )
        return json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        return repr(item)


def _build_reference_map(
    items: Sequence[Any],
    *,
    ignore_openai_conversation_item_ids: bool = False,
) -> dict[str, list[Any]]:
    """Map serialized keys to the concrete session items used to build them."""
    refs: dict[str, list[Any]] = {}
    for item in items:
        key = _session_item_key(
            item,
            ignore_openai_conversation_item_ids=ignore_openai_conversation_item_ids,
        )
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


def _build_frequency_map(
    items: Sequence[Any],
    *,
    ignore_openai_conversation_item_ids: bool = False,
) -> dict[str, int]:
    """Count how many times each serialized key appears in a collection."""
    freq: dict[str, int] = {}
    for item in items:
        key = _session_item_key(
            item,
            ignore_openai_conversation_item_ids=ignore_openai_conversation_item_ids,
        )
        freq[key] = freq.get(key, 0) + 1
    return freq
