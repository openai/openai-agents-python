from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from openai import AsyncOpenAI

from ..items import TResponseInputItem
from ..logger import log_model_and_tool_action_warning
from ..models._openai_shared import get_default_openai_client
from ..run_internal.items import (
    has_pending_tool_calls,
    normalize_input_items_for_api,
    sanitize_replayed_input_items,
)
from .openai_conversations_session import OpenAIConversationsSession
from .session import (
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionAwareSession,
    SessionABC,
    SessionInputCoverage,
)
from .session_settings import SessionSettings

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger("openai-agents.openai.compaction")

DEFAULT_COMPACTION_THRESHOLD = 10
_ALL_SESSION_ITEMS_LIMIT = 2_147_483_647

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

    _supports_compaction_metadata = True

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
                history. "auto" (default) uses a response ID only when its model input
                covered the full stored history, rebuilds from the full stored history when
                only a session limit truncated the input, and skips compaction when the
                input was transformed by callbacks, filters, handoffs, or resume.
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
            should_trigger_compaction or default_should_trigger_compaction
        )

        # cache for incremental candidate tracking
        self._compaction_candidate_items: list[TResponseInputItem] | None = None
        self._session_items: list[TResponseInputItem] | None = None
        self._last_processed_response_context: OpenAIResponsesCompactionArgs | None = None
        self._deferred_response_id: str | None = None
        self._last_unstored_response_id: str | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = get_default_openai_client() or AsyncOpenAI()
        return self._client

    @property
    def session_settings(self) -> SessionSettings | None:
        """Delegate session window settings to the wrapped store."""
        return getattr(self.underlying_session, "session_settings", None)

    @session_settings.setter
    def session_settings(self, value: SessionSettings | None) -> None:
        self.underlying_session.session_settings = value

    def _publish_response_context(
        self,
        response_id: str | None,
        input_coverage: SessionInputCoverage | None,
        reasoning_item_id_policy: Literal["preserve", "omit"] | None,
    ) -> None:
        if not response_id:
            return
        self._last_processed_response_context = {
            "response_id": response_id,
            # Never-established coverage publishes as "transformed" so a later call cannot
            # treat a response this attempt never proved safe as a compaction source.
            "input_coverage": input_coverage if input_coverage is not None else "transformed",
            "reasoning_item_id_policy": reasoning_item_id_policy,
        }

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

    async def run_compaction(self, args: OpenAIResponsesCompactionArgs | None = None) -> None:
        """Run compaction using responses.compact API."""
        response_id = args.get("response_id") if args else None
        reused_response_context = None
        if self._last_processed_response_context is not None and (
            response_id is None
            or response_id == self._last_processed_response_context["response_id"]
        ):
            reused_response_context = self._last_processed_response_context
            if response_id is None:
                # A manual call falls back to the last response this session processed.
                response_id = reused_response_context["response_id"]
        requested_mode = args.get("compaction_mode") if args else None
        mode = requested_mode or self.compaction_mode
        args_input_coverage = (
            args.get("input_coverage") if args and "input_coverage" in args else None
        )
        input_coverage = (
            args_input_coverage
            if args_input_coverage is not None
            else (
                reused_response_context.get("input_coverage")
                if reused_response_context is not None
                else None
            )
        )
        reasoning_item_id_policy = (
            args.get("reasoning_item_id_policy")
            if args and "reasoning_item_id_policy" in args
            else (
                reused_response_context.get("reasoning_item_id_policy")
                if reused_response_context is not None
                else None
            )
        )
        if args and "store" in args:
            store = args["store"]
            if store is False and response_id:
                self._last_unstored_response_id = response_id
            elif store is True and response_id == self._last_unstored_response_id:
                self._last_unstored_response_id = None
        else:
            store = None
        resolved_mode = self._resolve_compaction_mode_for_response(
            response_id=response_id,
            store=store,
            requested_mode=mode,
        )

        # Only an explicit previous_response_id resolves here without an id, and the auto
        # fallback below cannot rescue it, so fail before reading the session.
        if resolved_mode == "previous_response_id" and not response_id:
            raise ValueError(
                "OpenAIResponsesCompactionSession.run_compaction requires a response_id "
                "when using previous_response_id compaction."
            )

        if mode == "auto" and args_input_coverage == "transformed":
            # A transformed turn has no faithful compaction source, so preserve the store.
            # Only caller-reported coverage skips; manual calls keep released behavior.
            self._publish_response_context(response_id, "transformed", reasoning_item_id_policy)
            logger.debug(
                "skip: transformed model input for %s; auto compaction preserves the store",
                response_id,
            )
            return

        try:
            compaction_candidate_items, session_items = await self._ensure_compaction_candidates()

            used_auto_input_fallback = False
            if mode == "auto" and resolved_mode == "previous_response_id":
                input_coverage = await self._infer_input_coverage(input_coverage)
                if input_coverage != "full":
                    # An unproven response ID must never be reused as a compaction source.
                    resolved_mode = "input"
                    used_auto_input_fallback = True

            if used_auto_input_fallback and has_pending_tool_calls(session_items):
                # Sanitization would drop the pending call as an orphan, so defer. Publish
                # this attempt's coverage first: a later manual force must not reuse an
                # older context.
                self._publish_response_context(
                    response_id, input_coverage, reasoning_item_id_policy
                )
                await self._defer_compaction(
                    response_id or "",
                    store=store,
                    input_coverage=input_coverage,
                )
                return

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
                self._publish_response_context(
                    response_id, input_coverage, reasoning_item_id_policy
                )
                logger.debug(
                    "skip: decision hook declined compaction for %s (mode=%s)",
                    response_id,
                    resolved_mode,
                )
                return

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
                if used_auto_input_fallback:
                    # Replayed history must reach the API the way a model request would, so
                    # it cannot reintroduce items the successful turn deliberately dropped.
                    compact_kwargs["input"] = sanitize_replayed_input_items(
                        session_items, reasoning_item_id_policy
                    )
                else:
                    # Preserve the released explicit input-mode behavior.
                    compact_kwargs["input"] = session_items

            compacted = await self.client.responses.compact(**compact_kwargs)

            output_items = _strip_orphaned_assistant_ids(
                _normalize_compaction_output_items(compacted.output or [])
            )

            previous_items = await self._get_all_underlying_session_items()
            await self._replace_underlying_session_items(
                output_items=output_items,
                previous_items=previous_items,
            )
        except BaseException:
            # An aborted attempt still supersedes any older covered context a manual retry
            # could reuse; its unproven coverage then forces the input fallback.
            self._publish_response_context(response_id, "transformed", reasoning_item_id_policy)
            raise

        self._compaction_candidate_items = None
        self._session_items = None
        self._publish_response_context(response_id, input_coverage, reasoning_item_id_policy)

        logger.debug(
            "compact: done for %s (mode=%s, output=%s, candidates=%s)",
            response_id,
            resolved_mode,
            len(output_items),
            len(select_compaction_candidate_items(output_items)),
        )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        return await self.underlying_session.get_items(limit)

    async def _get_all_underlying_session_items(self) -> list[TResponseInputItem]:
        return await self.underlying_session.get_items(limit=_ALL_SESSION_ITEMS_LIMIT)

    async def _underlying_view_covers_history(self) -> bool:
        settings = getattr(self.underlying_session, "session_settings", None)
        limit = getattr(settings, "limit", None)
        if not isinstance(limit, int):
            return True

        prepared_view = await self.underlying_session.get_items()
        # A window filled exactly to its limit may have older items outside the view.
        return len(prepared_view) < limit

    async def _infer_input_coverage(
        self,
        input_coverage: SessionInputCoverage | None,
    ) -> SessionInputCoverage:
        if input_coverage is not None:
            return input_coverage
        # Without turn metadata the only detectable shortfall source is the session limit.
        return "full" if await self._underlying_view_covers_history() else "limit_only"

    async def _replace_underlying_session_items(
        self,
        *,
        output_items: list[TResponseInputItem],
        previous_items: list[TResponseInputItem],
    ) -> None:
        try:
            await self.underlying_session.clear_session()
        except Exception as clear_error:
            await self._restore_underlying_session_items_after_failed_clear(
                previous_items, clear_error
            )
            raise

        try:
            if output_items:
                await self.underlying_session.add_items(output_items)
        except Exception as replacement_error:
            await self._restore_underlying_session_items(previous_items, replacement_error)
            raise

    async def _restore_underlying_session_items_after_failed_clear(
        self,
        previous_items: list[TResponseInputItem],
        clear_error: Exception,
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
        replacement_error: Exception,
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

    async def _defer_compaction(
        self,
        response_id: str,
        store: bool | None = None,
        input_coverage: SessionInputCoverage | None = None,
    ) -> None:
        if self._deferred_response_id is not None:
            return
        mode = self.compaction_mode
        if mode == "auto" and input_coverage == "transformed":
            # A transformed turn must not schedule a later forced compaction either.
            return
        resolved_mode = self._resolve_compaction_mode_for_response(
            response_id=response_id,
            store=store,
            requested_mode=mode,
        )
        compaction_candidate_items, session_items = await self._ensure_compaction_candidates()
        if (
            mode == "auto"
            and resolved_mode == "previous_response_id"
            and await self._infer_input_coverage(input_coverage) == "limit_only"
        ):
            resolved_mode = "input"
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
        await self.underlying_session.add_items(items)
        if self._compaction_candidate_items is not None:
            new_items = _normalize_compaction_session_items(items)
            new_candidates = select_compaction_candidate_items(new_items)
            if new_candidates:
                self._compaction_candidate_items.extend(new_candidates)
        if self._session_items is not None:
            self._session_items.extend(_normalize_compaction_session_items(items))

    async def pop_item(self) -> TResponseInputItem | None:
        popped = await self.underlying_session.pop_item()
        if popped:
            self._compaction_candidate_items = None
            self._session_items = None
        return popped

    async def clear_session(self) -> None:
        await self.underlying_session.clear_session()
        self._compaction_candidate_items = []
        self._session_items = []
        self._deferred_response_id = None

    async def _ensure_compaction_candidates(
        self,
    ) -> tuple[list[TResponseInputItem], list[TResponseInputItem]]:
        """Lazy-load and cache compaction candidates."""
        if self._compaction_candidate_items is not None and self._session_items is not None:
            return (self._compaction_candidate_items[:], self._session_items[:])

        history = _normalize_compaction_session_items(
            await self._get_all_underlying_session_items()
        )
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
