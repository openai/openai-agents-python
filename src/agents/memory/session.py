from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeGuard, runtime_checkable

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from ..items import TResponseInputItem
    from ..run_context import RunContextWrapper
    from .session_settings import SessionSettings


def slice_items_by_turn(
    items: list[TResponseInputItem],
    turn_limit: int,
) -> list[TResponseInputItem]:
    """Return the latest ``turn_limit`` complete turns from ``items``.

    A turn is a user message plus every item that follows it up to the next user
    message. This helper walks backward from the newest item, counting
    user-message items as turn boundaries, and returns the slice that starts at
    the boundary of the ``turn_limit``-th newest turn. When fewer than
    ``turn_limit`` turns exist in ``items``, the full list is returned so no
    turn is ever split. A non-positive ``turn_limit`` returns an empty list,
    matching the non-positive ``limit`` semantics.

    Session implementations should use this helper when they support
    ``get_items(turn_limit=...)`` so the boundary logic stays consistent across
    backends.
    """
    if turn_limit <= 0:
        return []
    start = 0
    turns = 0
    index = len(items) - 1
    while index >= 0:
        item = items[index]
        if isinstance(item, dict) and item.get("role") == "user":
            turns += 1
            if turns == turn_limit:
                start = index
                break
        index -= 1
    return items[start:]


@runtime_checkable
class Session(Protocol):
    """Protocol for session implementations.

    Session stores conversation history for a specific session, allowing
    agents to maintain context without requiring explicit manual memory management.
    """

    session_id: str
    session_settings: SessionSettings | None = None

    async def get_items(
        self,
        limit: int | None = None,
        *,
        turn_limit: int | None = None,
    ) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.
            turn_limit: Maximum number of whole turns to retrieve. When specified,
                   returns the latest N complete turns, starting at a turn boundary so
                   a single turn's items are never split. If None, no turn boundary is
                   applied.

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
    async def get_items(
        self,
        limit: int | None = None,
        *,
        turn_limit: int | None = None,
    ) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.
            turn_limit: Maximum number of whole turns to retrieve. When specified,
                   returns the latest N complete turns, starting at a turn boundary so
                   a single turn's items are never split. If None, no turn boundary is
                   applied.

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


def _session_method_accepts_wrapper(method: Any) -> bool:
    """Return whether a session method opts into receiving ``wrapper``.

    The public ``Session`` protocol keeps its released signatures so existing structural
    implementations remain type-compatible. Custom sessions can opt in by adding a ``wrapper``
    parameter that can be passed by keyword.
    """
    try:
        parameters = inspect.signature(method).parameters.values()
    except Exception:
        return False

    return any(
        parameter.name == "wrapper"
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        for parameter in parameters
    )


def _session_accepts_wrapper(session: Any) -> bool:
    """Return whether every history operation accepts ``wrapper``."""
    try:
        methods = (
            session.get_items,
            session.add_items,
            session.pop_item,
            session.clear_session,
        )
    except Exception:
        return False
    return all(_session_method_accepts_wrapper(method) for method in methods)


def _get_session_wrapper(
    session: Any,
    wrapper: RunContextWrapper[Any] | None,
) -> RunContextWrapper[Any] | None:
    """Return ``wrapper`` only for sessions with a complete context-aware contract."""
    if wrapper is None or not _session_accepts_wrapper(session):
        return None
    return wrapper


async def _call_session_method(
    method: Any,
    /,
    *args: Any,
    wrapper: RunContextWrapper[Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Call a session method with its legacy shape unless it opts into ``wrapper``."""
    if wrapper is not None and _session_method_accepts_wrapper(method):
        kwargs["wrapper"] = wrapper
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result
