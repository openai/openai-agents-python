"""Internal owner for an already joined, successful plain root result."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from .rate_limits import ApiRateLimits
from .turn import Turn, _root_turn

if TYPE_CHECKING:
    from .agent import Agent
    from .input import UserInput
    from .observation import _ObservationBuffer
    from .session import Completed, _ChildCallbacks
    from .turn import SteeringReceipt
    from .usage import TokenUsageInfo

_OutputT = TypeVar("_OutputT")


class _CompletedTurnOwner:
    def __init__(
        self,
        *,
        opening_agent: Agent[Any, str],
        session_id: str,
        turn_id: str,
        submission_id: str,
        output: str,
        usage: TokenUsageInfo | None,
        observations: Callable[[], _ObservationBuffer],
    ) -> None:
        from .session import Completed, _future

        self.id = session_id
        self._loop = asyncio.get_running_loop()
        self._owner = object()
        self._opening_agent = opening_agent
        self._load_observations: Callable[[], _ObservationBuffer] | None = observations
        self._buffer: _ObservationBuffer | None = None
        completion = _future()
        turn = cast(Turn[str], _root_turn(self, turn_id, submission_id, completion))
        self._completed = Completed(turn, output, usage)
        completion.set_result(self._completed)

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Use Session in the event loop where it opened")

    def _check_owner(self, turn: Turn[Any]) -> None:
        from .session import StaleTurnError

        self._check_loop()
        if turn._owner is not self._owner:
            raise StaleTurnError("Submission belongs to a different Session instance")

    def _check_callback_operation(self, operation: str) -> None:
        from .session import _invocation

        invocation = _invocation.get()
        while invocation is not None:
            if invocation.active and invocation.session is self:
                raise RuntimeError(f"A tool callback cannot {operation}")
            invocation = invocation.parent

    @property
    def _observations(self) -> _ObservationBuffer:
        if self._buffer is None:
            factory = self._load_observations
            if factory is None:
                raise RuntimeError("Completed Turn observation factory is unavailable")
            buffer = factory()
            if buffer.loop is not self._loop or buffer.session_id != self.id or not buffer.closed:
                raise RuntimeError("Completed Turn requires its own closed observation buffer")
            buffer._callback_check = weakref.WeakMethod(self._check_callback_operation)
            self._buffer = buffer
            self._load_observations = None
        return self._buffer

    async def _wait(self, turn: Turn[_OutputT]) -> Completed[_OutputT]:
        self._check_owner(turn)
        return cast("Completed[_OutputT]", self._completed)

    async def _wait_for_request_or_result(self, turn: Turn[_OutputT]) -> Completed[_OutputT]:
        return await self._wait(turn)

    async def _steer(
        self, turn: Turn[Any], input: UserInput, *, expected_turn_id: str | None = None
    ) -> SteeringReceipt | None:
        self._check_owner(turn)
        raise RuntimeError("Session is closed")

    async def _cancel_turn(self, turn: Turn[Any], mode: Literal["graceful", "force"]) -> None:
        from .session import CapabilityError

        self._check_owner(turn)
        if turn.child_id is not None:
            raise CapabilityError("Use Subagent.try_cancel for a child Turn")
        if mode not in ("graceful", "force"):
            raise ValueError("mode must be 'graceful' or 'force'")

    def rate_limits(self) -> ApiRateLimits:
        self._check_loop()
        return ApiRateLimits()

    def _callbacks_for_child(self, turn: Turn[Any]) -> list[_ChildCallbacks]:
        return []
