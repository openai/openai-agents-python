from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from typing_extensions import TypedDict, TypeGuard

if TYPE_CHECKING:
    from ..items import TResponseInputItem
    from .session_settings import SessionSettings

SERVER_MANAGED_CONVERSATION_SESSION_ATTR = "_server_managed_conversation_session"


@runtime_checkable
class Session(Protocol):
    """Protocol for session implementations.

    Session stores conversation history for a specific session, allowing
    agents to maintain context without requiring explicit manual memory management.
    """

    session_id: str
    session_settings: SessionSettings | None = None

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        ...

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        ...

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        ...

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        ...


class SessionABC(ABC):
    """Abstract base class for session implementations.

    Session stores conversation history for a specific session, allowing
    agents to maintain context without requiring explicit manual memory management.

    This ABC is intended for internal use and as a base class for concrete implementations.
    Third-party libraries should implement the Session protocol instead.
    """

    session_id: str
    session_settings: SessionSettings | None = None

    @abstractmethod
    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        ...

    @abstractmethod
    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        ...

    @abstractmethod
    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        ...

    @abstractmethod
    async def clear_session(self) -> None:
        """Clear all items for this session."""
        ...


@runtime_checkable
class ServerManagedConversationSession(Session, Protocol):
    """Protocol for sessions whose canonical history is managed by a remote service."""

    _server_managed_conversation_session: Literal[True]


def is_server_managed_conversation_session(
    session: Session | None,
) -> TypeGuard[ServerManagedConversationSession]:
    """Check whether the session advertises server-managed history semantics."""
    if session is None:
        return False
    try:
        marker = getattr(session, SERVER_MANAGED_CONVERSATION_SESSION_ATTR, False)
    except Exception:
        return False
    return marker is True


class ReplaceFunctionCallSessionHistoryMutation(TypedDict):
    """Replace the canonical persisted function call for a tool call."""

    type: Literal["replace_function_call"]
    call_id: str
    replacement: TResponseInputItem


SessionHistoryMutation = ReplaceFunctionCallSessionHistoryMutation


class SessionHistoryRewriteArgs(TypedDict):
    """Arguments for persisted-history rewrites."""

    mutations: list[SessionHistoryMutation]


@runtime_checkable
class SessionHistoryRewriteAwareSession(Session, Protocol):
    """Protocol for sessions that can rewrite previously persisted history."""

    async def apply_history_mutations(self, args: SessionHistoryRewriteArgs) -> None:
        """Apply structured history mutations to the persisted session history."""
        ...


def is_session_history_rewrite_aware_session(
    session: Session | None,
) -> TypeGuard[SessionHistoryRewriteAwareSession]:
    """Check whether a session supports persisted-history rewrites."""
    if session is None:
        return False
    try:
        apply_history_mutations = getattr(session, "apply_history_mutations", None)
    except Exception:
        return False
    return callable(apply_history_mutations)


def apply_session_history_mutations(
    items: list[TResponseInputItem],
    mutations: list[SessionHistoryMutation],
) -> list[TResponseInputItem]:
    """Apply structured history mutations to a list of persisted session items."""
    next_items = [copy.deepcopy(item) for item in items]
    for mutation in mutations:
        if mutation["type"] == "replace_function_call":
            next_items = _apply_replace_function_call_mutation(next_items, mutation)
    return next_items


def _apply_replace_function_call_mutation(
    items: list[TResponseInputItem],
    mutation: ReplaceFunctionCallSessionHistoryMutation,
) -> list[TResponseInputItem]:
    """Replace the first matching function call and drop later duplicates for the same call id."""
    replacement = copy.deepcopy(mutation["replacement"])
    next_items: list[TResponseInputItem] = []
    kept_replacement = False

    for item in items:
        if _is_matching_function_call(item, mutation["call_id"]):
            if not kept_replacement:
                next_items.append(replacement)
                kept_replacement = True
            continue
        next_items.append(item)

    return next_items


def _is_matching_function_call(item: TResponseInputItem, call_id: str) -> bool:
    if isinstance(item, dict):
        return item.get("type") == "function_call" and item.get("call_id") == call_id
    item_type = getattr(item, "type", None)
    item_call_id = getattr(item, "call_id", None)
    return item_type == "function_call" and item_call_id == call_id


class OpenAIResponsesCompactionArgs(TypedDict, total=False):
    """Arguments for the run_compaction method."""

    response_id: str
    """The ID of the last response to use for compaction."""

    compaction_mode: Literal["previous_response_id", "input", "auto"]
    """How to provide history for compaction.

    - "auto": Use input when the last response was not stored or no response ID is available.
    - "previous_response_id": Use server-managed response history.
    - "input": Send locally stored session items as input.
    """

    store: bool
    """Whether the last model response was stored on the server.

    When set to False, compaction should avoid "previous_response_id" unless explicitly requested.
    """

    force: bool
    """Whether to force compaction even if the threshold is not met."""


@runtime_checkable
class OpenAIResponsesCompactionAwareSession(Session, Protocol):
    """Protocol for session implementations that support responses compaction."""

    async def run_compaction(self, args: OpenAIResponsesCompactionArgs | None = None) -> None:
        """Run the compaction process for the session."""
        ...


def is_openai_responses_compaction_aware_session(
    session: Session | None,
) -> TypeGuard[OpenAIResponsesCompactionAwareSession]:
    """Check if a session supports responses compaction."""
    if session is None:
        return False
    try:
        run_compaction = getattr(session, "run_compaction", None)
    except Exception:
        return False
    return callable(run_compaction)
