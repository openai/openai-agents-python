from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from openai import AsyncOpenAI

from ..items import TResponseInputItem
from ..models._openai_shared import get_default_openai_client
from ..run_internal.items import normalize_input_items_for_api
from .openai_conversations_session import OpenAIConversationsSession
from .session import (
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionAwareSession,
    SessionABC,
)

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger("openai-agents.openai.compaction")

DEFAULT_COMPACTION_THRESHOLD = 10

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
            should_trigger_compaction or default_should_trigger_compaction
        )

        # cache for incremental candidate tracking
        self._compaction_candidate_items: list[TResponseInputItem] | None = None
        self._session_items: list[TResponseInputItem] | None = None
        self._response_id: str | None = None
        self._deferred_response_id: str | None = None
        self._last_unstored_response_id: str | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = get_default_openai_client() or AsyncOpenAI()
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

    async def run_compaction(self, args: OpenAIResponsesCompactionArgs | None = None) -> None:
        """Run compaction using responses.compact API."""
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

        compaction_candidate_items, session_items = await self._ensure_compaction_candidates()

        force = args.get("force", False) if args else False
        should_compact = force or self.should_trigger_compaction(
            {
                "response_id": self._response_id,
                "compaction_mode": resolved_mode,
                "compaction_candidate_items": compaction_candidate_items,
                "session_items": session_items,
            }
        )

        if not should_compact:
            logger.debug(
                f"skip: decision hook declined compaction for {self._response_id} "
                f"(mode={resolved_mode})"
            )
            return

        self._deferred_response_id = None
        logger.debug(
            f"compact: start for {self._response_id} using {self.model} (mode={resolved_mode})"
        )

        compact_kwargs: dict[str, Any] = {"model": self.model}
        if resolved_mode == "previous_response_id":
            compact_kwargs["previous_response_id"] = self._response_id
        else:
            compact_kwargs["input"] = session_items

        compacted = await self.client.responses.compact(**compact_kwargs)

        output_items = _normalize_compaction_output_items(compacted.output or [])
        await self.underlying_session.clear_session()
        output_items = _strip_orphaned_assistant_ids(output_items)

        if output_items:
            await self.underlying_session.add_items(output_items)

        self._compaction_candidate_items = select_compaction_candidate_items(output_items)
        self._session_items = output_items

        logger.debug(
            f"compact: done for {self._response_id} "
            f"(mode={resolved_mode}, output={len(output_items)}, "
            f"candidates={len(self._compaction_candidate_items)})"
        )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        return await self.underlying_session.get_items(limit)

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

        history = _normalize_compaction_session_items(await self.underlying_session.get_items())
        candidates = select_compaction_candidate_items(history)
        self._compaction_candidate_items = candidates
        self._session_items = history

        logger.debug(
            f"candidates: initialized (history={len(history)}, candidates={len(candidates)})"
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
