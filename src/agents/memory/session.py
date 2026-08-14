from __future__ import annotations

import copy
import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeGuard, cast, runtime_checkable

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from ..items import TResponseInputItem
    from ..run_context import RunContextWrapper
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
    """Check whether a session advertises server-managed history semantics."""
    if session is None:
        return False
    try:
        marker = inspect.getattr_static(session, SERVER_MANAGED_CONVERSATION_SESSION_ATTR, False)
    except Exception:
        return False
    return marker is True


class ReplaceFunctionCallSessionHistoryMutation(TypedDict):
    """Replace the canonical persisted function call for a tool call."""

    type: Literal["replace_function_call"]
    call_id: str
    expected: TResponseInputItem
    replacement: TResponseInputItem


SessionHistoryMutation = ReplaceFunctionCallSessionHistoryMutation


class SessionHistoryRewriteArgs(TypedDict):
    """Arguments for persisted-history rewrites."""

    mutations: list[SessionHistoryMutation]


@runtime_checkable
class SessionHistoryRewriteAwareSession(Session, Protocol):
    """Protocol for sessions that can compare and rewrite persisted function calls."""

    supports_expected_history_mutations: Literal[True]

    async def apply_history_mutations(self, args: SessionHistoryRewriteArgs) -> bool:
        """Apply every expected mutation and confirm all targets were reconciled."""
        ...


def is_session_history_rewrite_aware_session(
    session: Session | None,
) -> TypeGuard[SessionHistoryRewriteAwareSession]:
    """Check whether a session supports expected persisted-history rewrites."""
    if session is None:
        return False
    try:
        apply_history_mutations = inspect.getattr_static(session, "apply_history_mutations", None)
        supports_expected_mutations = inspect.getattr_static(
            session, "supports_expected_history_mutations", False
        )
    except Exception:
        return False
    return callable(apply_history_mutations) and supports_expected_mutations is True


def apply_session_history_mutations(
    items: list[TResponseInputItem],
    mutations: list[SessionHistoryMutation],
) -> list[TResponseInputItem]:
    """Apply structured history mutations to a list of persisted session items."""
    next_items = list(items)
    for mutation in mutations:
        if mutation["type"] == "replace_function_call":
            next_items = _apply_replace_function_call_mutation(next_items, mutation)
    return next_items


def _apply_replace_function_call_mutation(
    items: list[TResponseInputItem],
    mutation: ReplaceFunctionCallSessionHistoryMutation,
) -> list[TResponseInputItem]:
    """Replace the latest expected call or accept an already-applied replacement."""
    call_id = mutation["call_id"]
    expected = _snapshot_matching_function_call(mutation["expected"], call_id)
    replacement = _snapshot_matching_function_call(mutation["replacement"], call_id)
    if expected is None or replacement is None:
        raise ValueError("Session history mutation contains an invalid function call.")

    for index in range(len(items) - 1, -1, -1):
        candidate = _snapshot_matching_function_call(items[index], call_id)
        if candidate is None:
            continue
        if candidate == replacement:
            return list(items)
        if candidate == expected:
            next_items = list(items)
            next_items[index] = cast(Any, replacement)
            return next_items
        break

    raise ValueError("Session history mutation target did not match the expected function call.")


def _snapshot_matching_function_call(item: Any, call_id: str) -> dict[str, Any] | None:
    """Snapshot a matching function call without inspecting unrelated history values."""
    if isinstance(item, dict):
        payload = item
    elif hasattr(item, "model_dump"):
        try:
            payload = item.model_dump(exclude_unset=True)
        except TypeError:
            payload = item.model_dump()
    else:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "function_call"
        or payload.get("call_id") != call_id
    ):
        return None
    return copy.deepcopy(payload)


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
