"""Live references to exact turns, shared by root sessions and subagents."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

if TYPE_CHECKING:
    from .input import UserInput
    from .manual import Interaction
    from .observation import EventStream
    from .session import Completed, Session

OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class PendingRequests(Generic[OutputT]):
    """Current requests of one Turn. Another consumer may answer first."""

    turn: Turn[OutputT]
    requests: tuple[Interaction, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, init=False)
class Turn(Generic[OutputT]):
    """One submitted root task, or a separately scoped native child turn.

    A root has no native ID until actual admission. It then retains that first
    ID through accepted steering continuations. Reading the result is passive,
    even after later work starts or the Session closes. Turns cannot be restored from IDs.
    """

    session_id: str
    turn_id: str | None = field(compare=False)
    submission_id: str
    child_id: str | None
    instance_id: int | None
    _session: Session[Any, Any] = field(repr=False, compare=False)
    _owner: object = field(repr=False, compare=False)
    _completion: asyncio.Future[Any] | None = field(repr=False, compare=False)
    _continuation: asyncio.Future[Turn[OutputT]] | None = field(repr=False, compare=False)
    _result: asyncio.Task[Completed[OutputT]] | None = field(repr=False, compare=False)
    _native: Any = field(repr=False, compare=False)

    def __new__(cls, *args: Any, **kwargs: Any) -> Turn[Any]:
        raise TypeError("Turns are issued by a Session or Subagent")

    def __reduce__(self) -> Any:
        raise TypeError("Turn belongs to a live Session and cannot be serialized")

    async def get_result(self) -> Completed[OutputT]:
        """Wait for all this root's work without cancelling it if a waiter stops."""
        return await self._session._wait(self)

    async def wait_for_request_or_result(self) -> Completed[OutputT] | PendingRequests[OutputT]:
        """Wait for this turn's result or its own live, answerable requests.

        Use Session.interactions() to also receive requests from other turns.
        Like get_result(), cancelling this waiter leaves the Turn alive. Only this
        root's owned continuation requests are included, never child requests.
        """
        return await self._session._wait_for_request_or_result(self)

    async def steer(self, input: UserInput) -> None:
        """Steer this submitted task's current native step, never another task."""
        await self._session._steer(self, input)

    async def cancel(self, *, mode: Literal["graceful", "force"] = "force") -> None:
        """Stop and join this root's callbacks and cells. A settled root is a no-op.

        Child turns require Subagent.try_cancel, whose guarantee is weaker.
        """
        await self._session._cancel_turn(self, mode)

    def events(self, *, after: int = 0) -> EventStream:
        """Observe owned steps with their native IDs. Gaps remain Session-global.

        TurnFinished describes an actual native step; get_result() is overall completion.
        """
        from .observation import EventStream

        self._session._check_owner(self)
        self._session._check_callback_operation("observe its own Session")
        return EventStream(self._session._observations, after=after, _turn=self)

    def _steps(self) -> Iterator[Turn[OutputT]]:
        step = self
        while True:
            yield step
            successor = step._continuation
            if (
                successor is None
                or not successor.done()
                or successor.cancelled()
                or successor.exception() is not None
            ):
                return
            step = successor.result()


def _root_turn(
    session: Session[Any, Any],
    turn_id: str | None,
    submission_id: str,
    completion: asyncio.Future[Any],
) -> Turn[Any]:
    result = object.__new__(Turn)
    for name, value in {
        "session_id": session.id,
        "turn_id": turn_id,
        "submission_id": submission_id,
        "child_id": None,
        "instance_id": None,
        "_session": session,
        "_owner": session._owner,
        "_completion": completion,
        "_continuation": None,
        "_result": None,
        "_native": None,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _child_turn(session: Session[Any, Any], native: Any) -> Turn[str]:
    incarnation = native.incarnation
    if incarnation is None:
        from .session import NativeError

        raise NativeError("Native child turn has no captured SDK incarnation")
    result = object.__new__(Turn)
    for name, value in {
        "session_id": session.id,
        "turn_id": native.turn_id,
        "submission_id": _child_submission_id(native.child_id, incarnation, native.turn_id),
        "child_id": native.child_id,
        "instance_id": native.instance_id,
        "_session": session,
        "_owner": session._owner,
        "_completion": None,
        "_continuation": None,
        "_result": None,
        "_native": native,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _child_submission_id(child_id: str, incarnation: str, turn_id: str) -> str:
    return f"child:{child_id}:{incarnation}:{turn_id}"
