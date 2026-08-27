from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from openai import AsyncOpenAI

from ..items import TResponseInputItem
from ..logger import log_model_and_tool_action_warning
from ..models._openai_shared import get_default_openai_client
from ..run_internal.items import normalize_input_items_for_api
from ..usage import _response_usage_to_usage
from .openai_conversations_session import OpenAIConversationsSession
from .session import (
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionAwareSession,
    SessionABC,
)

if TYPE_CHECKING:
    from ..run_context import RunContextWrapper
    from .session import Session

logger = logging.getLogger("openai-agents.openai.compaction")

DEFAULT_COMPACTION_THRESHOLD = 10
_ALL_SESSION_ITEMS_LIMIT = 2_147_483_647
# Recorded response boundaries are pruned oldest first past this size so ids whose
# compaction never runs (for example turns whose compaction was deferred) cannot
# grow the map without bound.
_MAX_RECORDED_RESPONSE_BOUNDARIES = 50

OpenAIResponsesCompactionMode = Literal["previous_response_id", "input", "auto"]


def select_compaction_candidate_items(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Select compaction candidate items.

    Excludes user messages and compaction items.
    """

    def _is_user_message(item: TResponseInputItem) -> bool:
        if not isinstance(item, dict):
            return False
        if item.get("type") == "message":
            return item.get("role") == "user"
        return item.get("role") == "user" and "content" in item

    return [
        item
        for item in items
        if not (
            _is_user_message(item) or (isinstance(item, dict) and item.get("type") == "compaction")
        )
    ]


def default_should_trigger_compaction(context: dict[str, Any]) -> bool:
    """Default decision: compact when >= 10 candidate items exist."""
    return len(context["compaction_candidate_items"]) >= DEFAULT_COMPACTION_THRESHOLD


def is_openai_model_name(model: str) -> bool:
    """Validate model name follows OpenAI conventions."""
    trimmed = model.strip()
    if not trimmed:
        return False

    # Handle fine-tuned models: ft:gpt-4.1:org:proj:suffix
    without_ft_prefix = trimmed[3:] if trimmed.startswith("ft:") else trimmed
    root = without_ft_prefix.split(":", 1)[0]

    # Allow gpt-* and o* models
    if root.startswith("gpt-"):
        return True
    if root.startswith("o") and root[1:2].isdigit():
        return True

    return False


class OpenAIResponsesCompactionSession(SessionABC, OpenAIResponsesCompactionAwareSession):
    """Session decorator that triggers responses.compact when stored history grows.

    Works with OpenAI Responses API models only. Wraps any Session (except
    OpenAIConversationsSession) and automatically calls the OpenAI responses.compact
    API after each turn when the decision hook returns True.
    """

    def __init__(
        self,
        session_id: str,
        underlying_session: Session,
        *,
        client: AsyncOpenAI | None = None,
        model: str = "gpt-4.1",
        compaction_mode: OpenAIResponsesCompactionMode = "auto",
        should_trigger_compaction: Callable[[dict[str, Any]], bool] | None = None,
    ):
        """Initialize the compaction session.

        Args:
            session_id: Identifier for this session.
            underlying_session: Session store that holds the compacted history. Cannot be
                OpenAIConversationsSession.
            client: OpenAI client for responses.compact API calls. Defaults to
                get_default_openai_client() or new AsyncOpenAI().
            model: Model to use for responses.compact. Defaults to "gpt-4.1". Must be an
                OpenAI model name (gpt-*, o*, or ft:gpt-*).
            compaction_mode: Controls how the compaction request provides conversation
                history. "auto" (default) uses input when the last response was not
                stored or no response_id is available.
            should_trigger_compaction: Custom decision hook. Defaults to triggering when
                10+ compaction candidates exist.
        """
        if isinstance(underlying_session, OpenAIConversationsSession):
            raise ValueError(
                "OpenAIResponsesCompactionSession cannot wrap OpenAIConversationsSession "
                "because it manages its own history on the server."
            )

        if not is_openai_model_name(model):
            raise ValueError(f"Unsupported model for OpenAI responses compaction: {model}")

        self.session_id = session_id
        self.underlying_session = underlying_session
        self._client = client
        self.model = model
        self.compaction_mode = compaction_mode
        self.should_trigger_compaction = (
            should_trigger_compaction
            if should_trigger_compaction is not None
            else default_should_trigger_compaction
        )

        # cache for incremental candidate tracking
        self._compaction_candidate_items: list[TResponseInputItem] | None = None
        self._session_items: list[TResponseInputItem] | None = None
        self._response_id: str | None = None
        # Each response id is paired with the exact local item count at the moment
        # its batch was persisted, recorded under the mutation lock. A
        # previous_response_id compaction covers the server side history through
        # that response only, so the replacement must preserve every local item
        # past the recorded boundary, not past a snapshot taken later. A None
        # value is a tombstone: the response was recorded, but a later
        # replacement rewrote the prefix its boundary counted, so no count on
        # the current history describes that response's coverage and a
        # compaction keyed on it must skip instead of guessing.
        self._response_boundaries: dict[str, int | None] = {}
        # True once any response boundary has been recorded on this instance.
        # Eviction past the cap and the wipes in clear_session and pop_item
        # can drop the entry for a response whose compaction is still pending,
        # so an absent id alone cannot distinguish such a response from a
        # direct caller that never records boundaries. The flag keeps that
        # distinction, and it is never reset: a compaction keyed on a response
        # recorded before a clear must still skip after the clear.
        self._response_boundaries_ever_recorded = False
        self._deferred_response_id: str | None = None
        self._last_unstored_response_id: str | None = None
        # Serialize wrapper mutations against compaction snapshot/replace/restore so a
        # cancellation rollback cannot rewrite past a newer concurrent write.
        self._mutation_lock = asyncio.Lock()
        # Bumped under the lock by every rewrite of stored history: clear_session,
        # pop_item, and a successful compaction replacement. The prefix check in
        # run_compaction cannot see a rewrite when the snapshot it compares against
        # was empty, so the counter records those explicitly.
        self._destructive_generation = 0

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            default_client = get_default_openai_client()
            self._client = default_client if default_client is not None else AsyncOpenAI()
        return self._client

    def _resolve_compaction_mode_for_response(
        self,
        *,
        response_id: str | None,
        store: bool | None,
        requested_mode: OpenAIResponsesCompactionMode | None,
    ) -> _ResolvedCompactionMode:
        mode = requested_mode or self.compaction_mode
        if (
            mode == "auto"
            and store is None
            and response_id is not None
            and response_id == self._last_unstored_response_id
        ):
            return "input"
        return _resolve_compaction_mode(mode, response_id=response_id, store=store)

    async def run_compaction(
        self,
        args: OpenAIResponsesCompactionArgs | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        """Run compaction using responses.compact API.

        When a run context is provided, the billed compaction request contributes to
        that run's usage totals.
        """
        if args and args.get("response_id"):
            self._response_id = args["response_id"]
        requested_mode = args.get("compaction_mode") if args else None
        if args and "store" in args:
            store = args["store"]
            if store is False and self._response_id:
                self._last_unstored_response_id = self._response_id
            elif store is True and self._response_id == self._last_unstored_response_id:
                self._last_unstored_response_id = None
        else:
            store = None
        resolved_mode = self._resolve_compaction_mode_for_response(
            response_id=self._response_id,
            store=store,
            requested_mode=requested_mode,
        )

        if resolved_mode == "previous_response_id" and not self._response_id:
            raise ValueError(
                "OpenAIResponsesCompactionSession.run_compaction requires a response_id "
                "when using previous_response_id compaction."
            )

        # resolved_mode was derived from the response id as it is right now. A
        # concurrent call can overwrite self._response_id while this one waits on
        # the lock below, so everything after this line uses the paired local.
        response_id = self._response_id

        async with self._mutation_lock:
            compaction_candidate_items, session_items = await self._ensure_compaction_candidates()

            force = args.get("force", False) if args else False
            should_compact = force or self.should_trigger_compaction(
                {
                    "response_id": response_id,
                    "compaction_mode": resolved_mode,
                    "compaction_candidate_items": compaction_candidate_items,
                    "session_items": session_items,
                }
            )

            if not should_compact:
                logger.debug(
                    "skip: decision hook declined compaction for %s (mode=%s)",
                    response_id,
                    resolved_mode,
                )
                return

            # The boundary recorded when this response's batch was persisted is
            # the authority on what previous_response_id compaction covers. The
            # snapshot below can already contain turns another run appended after
            # that persist, so it only anchors the divergence check and never the
            # ownership boundary. clear_session and pop_item drop recorded
            # boundaries outright and a successful replacement translates the
            # survivors, so a stale count can never be read here. A tombstone
            # means an earlier replacement rewrote the prefix this boundary
            # counted; the snapshot fallback would classify turns persisted
            # after this response into its baseline and drop them, so skip. An
            # absent entry is ambiguous once anything was ever recorded:
            # eviction past the cap and the wipes in clear_session and
            # pop_item drop entries for responses whose compactions may still
            # be pending, and guessing from the snapshot would claim newer
            # turns for such a response, so those skip like tombstones. The
            # snapshot fallback stays reserved for sessions that never record
            # boundaries, whose direct callers persist before compacting and
            # own that ordering.
            recorded_boundary: int | None = None
            if resolved_mode == "previous_response_id" and response_id is not None:
                if response_id in self._response_boundaries:
                    recorded_boundary = self._response_boundaries[response_id]
                    if recorded_boundary is None:
                        logger.warning(
                            "Skipped compaction for %s (mode=%s): an earlier replacement "
                            "rewrote the history prefix this response's boundary counted, "
                            "so no boundary on the current history describes its coverage.",
                            response_id,
                            resolved_mode,
                        )
                        return
                elif self._response_boundaries_ever_recorded:
                    logger.warning(
                        "Skipped compaction for %s (mode=%s): this session records response "
                        "boundaries but has no entry for this response, so its boundary was "
                        "evicted or removed and the snapshot fallback would claim turns "
                        "persisted after this response.",
                        response_id,
                        resolved_mode,
                    )
                    return

            # Capture the full stored history while the lock still excludes writers.
            # It anchors the post-flight check that detects concurrent mutations.
            snapshot_items = await self._get_all_underlying_session_items()
            snapshot_generation = self._destructive_generation

        self._deferred_response_id = None
        logger.debug(
            "compact: start for %s using %s (mode=%s)",
            response_id,
            self.model,
            resolved_mode,
        )

        compact_kwargs: dict[str, Any] = {"model": self.model}
        if resolved_mode == "previous_response_id":
            compact_kwargs["previous_response_id"] = response_id
        else:
            compact_kwargs["input"] = session_items

        compacted = await self.client.responses.compact(**compact_kwargs)

        compacted_usage = getattr(compacted, "usage", None)
        if wrapper is not None and compacted_usage is not None:
            wrapper.usage.add(_response_usage_to_usage(compacted_usage))

        output_items = _strip_orphaned_assistant_ids(
            _normalize_compaction_output_items(compacted.output or [])
        )

        async with self._mutation_lock:
            previous_items = await self._get_all_underlying_session_items()
            baseline_count = len(snapshot_items)
            if (
                self._destructive_generation != snapshot_generation
                or previous_items[:baseline_count] != snapshot_items
            ):
                # A concurrent clear_session or pop_item rewrote history while the
                # compaction request was in flight, so the snapshot no longer
                # describes the stored items. Replacing them would resurrect
                # deleted history; keep the current items and drop the caches. The
                # generation counter catches the case the prefix check cannot: a
                # clear_session while the snapshot itself was empty.
                self._compaction_candidate_items = None
                self._session_items = None
                logger.warning(
                    "Skipped compaction replacement for %s (mode=%s): session history "
                    "diverged from the compaction snapshot while the request was in flight.",
                    response_id,
                    resolved_mode,
                )
                return
            # Preserve every item past the recorded boundary for this response.
            # Turns another run appended between this response's persisted batch
            # and the snapshot are not covered by previous_response_id compaction,
            # so they must survive alongside items appended while the request was
            # in flight. Without a recorded boundary (direct run_compaction calls,
            # or input mode where the compaction input was captured under the same
            # lock as the snapshot) the snapshot length is the boundary.
            preserve_from = recorded_boundary if recorded_boundary is not None else baseline_count
            concurrent_tail = previous_items[preserve_from:]
            await self._replace_underlying_session_items(
                output_items=output_items + concurrent_tail,
                previous_items=previous_items,
            )
            cached_items = output_items + _normalize_compaction_session_items(concurrent_tail)
            self._compaction_candidate_items = select_compaction_candidate_items(cached_items)
            self._session_items = cached_items
            # The replacement itself rewrote stored history, so a second in flight
            # compaction that snapshotted before it must not treat this output as
            # a concurrent tail. With a non empty snapshot the prefix check would
            # catch that; when both snapshots were empty only the counter can.
            self._destructive_generation += 1
            # Translate every recorded boundary onto the rewritten history. The
            # replacement rewrote previous_items[:preserve_from] into output_items
            # and kept the tail, so a boundary at or past preserve_from still
            # counts the same persisted batch at its shifted position, and an
            # overlapping compaction keyed on that response stays sound. A
            # boundary inside the rewritten prefix has no counterpart in the new
            # history, and any count would claim newer items for that response;
            # keep a tombstone so its compaction skips instead.
            self._response_boundaries = {
                rid: (
                    boundary - preserve_from + len(output_items)
                    if boundary is not None and boundary >= preserve_from
                    else None
                )
                for rid, boundary in self._response_boundaries.items()
            }

        logger.debug(
            "compact: done for %s (mode=%s, output=%s, candidates=%s, concurrent_tail=%s)",
            response_id,
            resolved_mode,
            len(output_items),
            len(self._compaction_candidate_items or []),
            len(concurrent_tail),
        )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        return await self.underlying_session.get_items(limit)

    async def _get_all_underlying_session_items(self) -> list[TResponseInputItem]:
        return await self.underlying_session.get_items(limit=_ALL_SESSION_ITEMS_LIMIT)

    async def _replace_underlying_session_items(
        self,
        *,
        output_items: list[TResponseInputItem],
        previous_items: list[TResponseInputItem],
    ) -> None:
        # Treat clear → add as one replacement transaction. Exception and CancelledError
        # both restore previous history, and restore settlement is always drained so a
        # cancel during restore cannot leave an empty session.
        cleared = False
        try:
            await self.underlying_session.clear_session()
            cleared = True
            if output_items:
                await self.underlying_session.add_items(output_items)
        except Exception as error:
            await self._recover_from_failed_replacement(
                previous_items=previous_items,
                error=error,
                cleared=cleared,
            )
            raise
        except asyncio.CancelledError as error:
            await self._recover_from_failed_replacement(
                previous_items=previous_items,
                error=error,
                cleared=cleared,
            )
            raise

    async def _recover_from_failed_replacement(
        self,
        *,
        previous_items: list[TResponseInputItem],
        error: BaseException,
        cleared: bool,
    ) -> None:
        if not cleared:
            restore = self._restore_underlying_session_items_after_failed_clear(
                previous_items, error
            )
        else:
            restore = self._restore_underlying_session_items(previous_items, error)
        await self._await_restore_despite_cancellation(restore)

    async def _await_restore_despite_cancellation(self, restore: Awaitable[None]) -> None:
        """Await restore even when the current task keeps receiving cancellation.

        ``asyncio.shield`` alone is not enough: a second ``task.cancel()`` makes
        ``await asyncio.shield(restore)`` raise immediately while restore is still
        running. Keep re-awaiting the shielded task until it settles, then
        re-raise ``CancelledError`` so callers still observe cancellation.
        """
        restore_task = asyncio.ensure_future(restore)
        try:
            await asyncio.shield(restore_task)
        except asyncio.CancelledError:
            while not restore_task.done():
                try:
                    await asyncio.shield(restore_task)
                except asyncio.CancelledError:
                    continue
            # Retrieve the restore outcome so a failed restore does not warn about an
            # unretrieved task exception after we re-raise cancellation.
            _ = restore_task.exception() if not restore_task.cancelled() else None
            raise

    async def _restore_underlying_session_items_after_failed_clear(
        self,
        previous_items: list[TResponseInputItem],
        clear_error: BaseException,
    ) -> None:
        try:
            current_items = await self._get_all_underlying_session_items()
        except Exception as inspection_error:
            log_model_and_tool_action_warning(
                logger,
                "Failed to inspect session history after compaction replacement clear failed.",
                inspection_error,
            )
            return

        if current_items == previous_items:
            return

        await self._restore_underlying_session_items(
            previous_items, clear_error, clear_existing_items=False
        )

    async def _restore_underlying_session_items(
        self,
        previous_items: list[TResponseInputItem],
        replacement_error: BaseException,
        *,
        clear_existing_items: bool = True,
    ) -> None:
        try:
            if clear_existing_items:
                await self.underlying_session.clear_session()
            if previous_items:
                await self.underlying_session.add_items(list(previous_items))
        except Exception as restore_error:
            log_model_and_tool_action_warning(
                logger,
                "Failed to restore session history after compaction replacement failed.",
                restore_error,
            )
            return

        log_model_and_tool_action_warning(
            logger,
            "Restored previous session history after compaction replacement failed",
            replacement_error,
        )

    async def _defer_compaction(self, response_id: str, store: bool | None = None) -> None:
        if self._deferred_response_id is not None:
            return
        compaction_candidate_items, session_items = await self._ensure_compaction_candidates()
        resolved_mode = self._resolve_compaction_mode_for_response(
            response_id=response_id,
            store=store,
            requested_mode=None,
        )
        should_compact = self.should_trigger_compaction(
            {
                "response_id": response_id,
                "compaction_mode": resolved_mode,
                "compaction_candidate_items": compaction_candidate_items,
                "session_items": session_items,
            }
        )
        if should_compact:
            self._deferred_response_id = response_id

    def _get_deferred_compaction_response_id(self) -> str | None:
        return self._deferred_response_id

    def _clear_deferred_compaction(self) -> None:
        self._deferred_response_id = None

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        async with self._mutation_lock:
            await self._add_items_locked(items)

    async def _add_items_for_response(
        self, items: list[TResponseInputItem], *, response_id: str
    ) -> None:
        """Append a response's persisted batch and record its compaction boundary.

        The runner calls this instead of add_items when the batch belongs to a
        specific response. Recording the boundary in the same locked region as
        the append keeps the pairing exact: no other writer can slip items in
        between the batch and the count recorded for it, so run_compaction can
        later preserve everything past the boundary regardless of what other
        runs appended before its own snapshot.

        The count that seeds the boundary is read before the append. The lock
        excludes every other writer for the whole region, so that count plus
        the batch length equals the count a read after the append would return,
        and once the append succeeds the recording below is pure assignment
        that cannot fail. A history read that failed after a successful append
        would surface an error for a batch the backend already holds, and
        retrying the turn would persist it again. When the read itself fails,
        the append has not started, so no boundary is recorded, the ever
        recorded flag stays untouched, and a retry begins clean.
        """
        async with self._mutation_lock:
            boundary = len(await self._get_all_underlying_session_items()) + len(items)
            await self._add_items_locked(items)
            self._response_boundaries.pop(response_id, None)
            self._response_boundaries[response_id] = boundary
            self._response_boundaries_ever_recorded = True
            while len(self._response_boundaries) > _MAX_RECORDED_RESPONSE_BOUNDARIES:
                del self._response_boundaries[next(iter(self._response_boundaries))]

    async def _add_items_locked(self, items: list[TResponseInputItem]) -> None:
        try:
            await self.underlying_session.add_items(items)
        except (Exception, asyncio.CancelledError):
            # The backend may have committed before acknowledgement failed. Read its
            # authoritative history again before compaction instead of retaining a
            # stale cache.
            self._compaction_candidate_items = None
            self._session_items = None
            raise
        if self._compaction_candidate_items is not None:
            new_items = _normalize_compaction_session_items(items)
            new_candidates = select_compaction_candidate_items(new_items)
            if new_candidates:
                self._compaction_candidate_items.extend(new_candidates)
        if self._session_items is not None:
            self._session_items.extend(_normalize_compaction_session_items(items))

    async def pop_item(self) -> TResponseInputItem | None:
        async with self._mutation_lock:
            popped = await self.underlying_session.pop_item()
            if popped:
                self._compaction_candidate_items = None
                self._session_items = None
                self._destructive_generation += 1
                # Recorded boundaries are item counts, so removing an item makes
                # them stale even when the pop only trimmed the tail.
                self._response_boundaries.clear()
            return popped

    async def clear_session(self) -> None:
        async with self._mutation_lock:
            await self.underlying_session.clear_session()
            self._compaction_candidate_items = []
            self._session_items = []
            self._deferred_response_id = None
            self._destructive_generation += 1
            self._response_boundaries.clear()

    async def _ensure_compaction_candidates(
        self,
    ) -> tuple[list[TResponseInputItem], list[TResponseInputItem]]:
        """Lazy-load and cache compaction candidates."""
        if self._compaction_candidate_items is not None and self._session_items is not None:
            return (self._compaction_candidate_items[:], self._session_items[:])

        history = _normalize_compaction_session_items(await self.underlying_session.get_items())
        candidates = select_compaction_candidate_items(history)
        self._compaction_candidate_items = candidates
        self._session_items = history

        logger.debug(
            "candidates: initialized (history=%s, candidates=%s)",
            len(history),
            len(candidates),
        )
        return (candidates[:], history[:])


def _strip_orphaned_assistant_ids(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Remove ``id`` from assistant messages when their paired reasoning items are missing.

    Some models (e.g. gpt-5.4) return compacted output that retains assistant
    message IDs even after stripping the reasoning items those IDs reference.
    Sending these orphaned IDs back to ``responses.create`` causes a 400 error
    because the API expects the paired reasoning item for each assistant message
    ID.  This function detects and removes those orphaned IDs so the compacted
    history can be used safely.
    """
    if not items:
        return items

    has_reasoning = any(
        isinstance(item, dict) and item.get("type") == "reasoning" for item in items
    )
    if has_reasoning:
        return items

    cleaned: list[TResponseInputItem] = []
    for item in items:
        if isinstance(item, dict) and item.get("role") == "assistant" and "id" in item:
            item = {k: v for k, v in item.items() if k != "id"}  # type: ignore[assignment]
        cleaned.append(item)
    return cleaned


def _normalize_compaction_output_items(items: list[Any]) -> list[TResponseInputItem]:
    """Normalize compacted output into replay-safe Responses input items."""
    output_items: list[TResponseInputItem] = []
    for item in items:
        if isinstance(item, dict):
            output_item = item
        else:
            # Suppress Pydantic literal warnings: responses.compact can return
            # user-style input_text content inside ResponseOutputMessage.
            output_item = item.model_dump(exclude_unset=True, warnings=False)

        if (
            isinstance(output_item, dict)
            and output_item.get("type") == "message"
            and output_item.get("role") == "user"
        ):
            output_items.append(_normalize_compaction_user_message(output_item))
            continue

        output_items.append(cast(TResponseInputItem, output_item))
    return output_items


def _normalize_compaction_user_message(item: dict[str, Any]) -> TResponseInputItem:
    """Normalize compacted user message content before it is reused as input."""
    content = item.get("content")
    if not isinstance(content, list):
        return cast(TResponseInputItem, item)

    normalized_content: list[Any] = []
    for content_item in content:
        if not isinstance(content_item, dict):
            normalized_content.append(content_item)
            continue

        content_type = content_item.get("type")
        if content_type == "input_image":
            normalized_content.append(_normalize_compaction_input_image(content_item))
        elif content_type == "input_file":
            normalized_content.append(_normalize_compaction_input_file(content_item))
        else:
            normalized_content.append(content_item)

    normalized_item = dict(item)
    normalized_item["content"] = normalized_content
    return cast(TResponseInputItem, normalized_item)


def _normalize_compaction_input_image(content_item: dict[str, Any]) -> dict[str, Any]:
    """Return a valid replay shape for a compacted Responses image input."""
    normalized = {"type": "input_image"}

    image_url = content_item.get("image_url")
    file_id = content_item.get("file_id")
    if isinstance(image_url, str) and image_url:
        normalized["image_url"] = image_url
    elif isinstance(file_id, str) and file_id:
        normalized["file_id"] = file_id
    else:
        raise ValueError("Compaction input_image item missing image_url or file_id.")

    detail = content_item.get("detail")
    if isinstance(detail, str) and detail:
        normalized["detail"] = detail

    return normalized


def _normalize_compaction_input_file(content_item: dict[str, Any]) -> dict[str, Any]:
    """Return a valid replay shape for a compacted Responses file input."""
    normalized = {"type": "input_file"}

    file_data = content_item.get("file_data")
    file_url = content_item.get("file_url")
    file_id = content_item.get("file_id")
    if isinstance(file_data, str) and file_data:
        normalized["file_data"] = file_data
    elif isinstance(file_url, str) and file_url:
        normalized["file_url"] = file_url
    elif isinstance(file_id, str) and file_id:
        normalized["file_id"] = file_id
    else:
        raise ValueError("Compaction input_file item missing file_data, file_url, or file_id.")

    filename = content_item.get("filename")
    if isinstance(filename, str) and filename:
        normalized["filename"] = filename

    detail = content_item.get("detail")
    if isinstance(detail, str) and detail:
        normalized["detail"] = detail

    return normalized


def _normalize_compaction_session_items(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Normalize compaction input so SDK-only metadata never reaches responses.compact."""
    return normalize_input_items_for_api(list(items))


_ResolvedCompactionMode = Literal["previous_response_id", "input"]


def _resolve_compaction_mode(
    requested_mode: OpenAIResponsesCompactionMode,
    *,
    response_id: str | None,
    store: bool | None,
) -> _ResolvedCompactionMode:
    if requested_mode != "auto":
        return requested_mode
    if store is False:
        return "input"
    if not response_id:
        return "input"
    return "previous_response_id"
