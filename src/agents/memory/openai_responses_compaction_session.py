from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from openai import AsyncOpenAI

from ..models._openai_shared import get_default_openai_client
from .openai_conversations_session import OpenAIConversationsSession
from .session import (
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionAwareSession,
    SessionABC,
)

if TYPE_CHECKING:
    from ..items import TResponseInputItem
    from .session import Session

logger = logging.getLogger("openai-agents.openai.compaction")

DEFAULT_COMPACTION_THRESHOLD = 10


def select_compaction_candidate_items(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Select items that are candidates for compaction.

    Excludes:
    - User messages (type=message, role=user)
    - Compaction items (type=compaction)
    """
    return [
        item
        for item in items
        if not (
            (item.get("type") == "message" and item.get("role") == "user")
            or item.get("type") == "compaction"
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

    # Handle fine-tuned models: ft:gpt-4o-mini:org:proj:suffix
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

    Wraps any Session (except OpenAIConversationsSession) and automatically calls
    the OpenAI responses.compact API after each turn when the decision hook returns True.
    """

    def __init__(
        self,
        session_id: str,
        underlying_session: Session,
        *,
        client: AsyncOpenAI | None = None,
        model: str = "gpt-4o",
        should_trigger_compaction: Callable[[dict[str, Any]], bool] | None = None,
    ):
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
        self.should_trigger_compaction = (
            should_trigger_compaction or default_should_trigger_compaction
        )

        # Cache for incremental candidate tracking
        self._compaction_candidate_items: list[TResponseInputItem] | None = None
        self._session_items: list[TResponseInputItem] | None = None
        self._response_id: str | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = get_default_openai_client() or AsyncOpenAI()
        return self._client

    async def run_compaction(self, args: OpenAIResponsesCompactionArgs | None = None) -> None:
        """Run compaction using responses.compact API."""
        if args and args.get("response_id"):
            self._response_id = args["response_id"]

        if not self._response_id:
            raise ValueError(
                "OpenAIResponsesCompactionSession.run_compaction requires a response_id"
            )

        # Get compaction candidates
        compaction_candidate_items, session_items = await self._ensure_compaction_candidates()

        # Check if should compact
        force = args.get("force", False) if args else False
        should_compact = force or self.should_trigger_compaction(
            {
                "response_id": self._response_id,
                "compaction_candidate_items": compaction_candidate_items,
                "session_items": session_items,
            }
        )

        if not should_compact:
            logger.debug(f"skip: decision hook declined compaction for {self._response_id}")
            return

        logger.debug(f"compact: start for {self._response_id} using {self.model}")

        # Call OpenAI responses.compact API
        compacted = await self.client.responses.compact(
            previous_response_id=self._response_id,
            model=self.model,
        )

        # Replace entire session with compacted output
        await self.underlying_session.clear_session()
        output_items: list[TResponseInputItem] = []
        if compacted.output:
            # We assume output items from API are compatible with input items (dicts)
            # or we cast them accordingly. The SDK types usually allow this.
            for item in compacted.output:
                if isinstance(item, dict):
                    output_items.append(item)
                else:
                    output_items.append(item.model_dump(exclude_unset=True))  # type: ignore

        if output_items:
            await self.underlying_session.add_items(output_items)

        # Update caches
        self._compaction_candidate_items = select_compaction_candidate_items(output_items)
        self._session_items = output_items

        logger.debug(
            f"compact: done for {self._response_id} "
            f"(output={len(output_items)}, candidates={len(self._compaction_candidate_items)})"
        )

    # Delegate all Session methods to underlying_session
    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        return await self.underlying_session.get_items(limit)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        await self.underlying_session.add_items(items)
        # Update caches incrementally
        if self._compaction_candidate_items is not None:
            new_candidates = select_compaction_candidate_items(items)
            if new_candidates:
                self._compaction_candidate_items.extend(new_candidates)
        if self._session_items is not None:
            self._session_items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        popped = await self.underlying_session.pop_item()
        # Invalidate caches on pop (simple approach)
        if popped:
            self._compaction_candidate_items = None
            self._session_items = None
        return popped

    async def clear_session(self) -> None:
        await self.underlying_session.clear_session()
        self._compaction_candidate_items = []
        self._session_items = []

    async def _ensure_compaction_candidates(
        self,
    ) -> tuple[list[TResponseInputItem], list[TResponseInputItem]]:
        """Lazy-load and cache compaction candidates."""
        if self._compaction_candidate_items is not None and self._session_items is not None:
            return (self._compaction_candidate_items[:], self._session_items[:])

        history = await self.underlying_session.get_items()
        candidates = select_compaction_candidate_items(history)
        self._compaction_candidate_items = candidates
        self._session_items = history

        logger.debug(
            f"candidates: initialized (history={len(history)}, candidates={len(candidates)})"
        )
        return (candidates[:], history[:])
