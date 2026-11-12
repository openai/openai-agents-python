"""Private admission queue for roots that have not entered execution."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ._lifecycle import _error_contexts, _retain_cleanup_error
from ._submission import _Submission

if TYPE_CHECKING:
    from .turn import Turn


class _QueuedRoots:
    def __init__(self, notify: Callable[[], None]) -> None:
        self._entries: deque[tuple[_Submission, list[dict[str, Any]]]] = deque()
        self._notify = notify

    def __bool__(self) -> bool:
        return bool(self._entries)

    def register(self, state: _Submission, parts: list[dict[str, Any]]) -> None:
        self._entries.append((state, parts))

    def contains(self, turn: Turn[Any]) -> bool:
        return any(state.issued is turn for state, _ in self._entries)

    def select(self) -> tuple[_Submission, list[dict[str, Any]]]:
        return self._entries.popleft()

    def withdraw(self, turn: Turn[Any], error: BaseException) -> None:
        for entry in self._entries:
            state, _ = entry
            if state.issued is turn:
                self._entries.remove(entry)
                self._reject(state, error, cancelled=True)
                self._notify()
                return

    def discard(self, error: BaseException, *, cancelled: bool = False) -> None:
        while self._entries:
            state, _ = self._entries.popleft()
            self._reject(state, error, cancelled=cancelled)
        self._notify()

    @staticmethod
    def _reject(state: _Submission, error: BaseException, *, cancelled: bool) -> None:
        tracing, state.tracing = state.tracing, None
        if tracing is not None:
            try:
                tracing.close(error, cancelled=cancelled)
            except BaseException as cleanup:
                _retain_cleanup_error(error, cleanup, _error_contexts(error.__context__))
        state.accepted.set_exception(error)
        state.completion.set_exception(error)
