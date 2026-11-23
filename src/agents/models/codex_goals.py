"""Paused goals for application-owned local Codex profiles."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ..exceptions import UserError
from ..memory.session import await_session_mutation

__all__ = ["CodexGoal", "GoalThread", "open_goal"]


class CodexGoal(BaseModel):
    """Native goal snapshot. Times are in seconds and timestamps use Unix time."""

    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, frozen=True, strict=True
    )
    thread_id: str
    objective: str
    status: Literal["paused", "budgetLimited", "active", "complete", "blocked", "usageLimited"]
    token_budget: int | None = None
    tokens_used: int
    time_used_seconds: int
    created_at: int
    updated_at: int


def _check_profile(profile: Path) -> None:
    if not profile.is_absolute() or profile.resolve(strict=True) != profile or not profile.is_dir():
        raise ValueError("Expected an existing, absolute local profile directory")


def _authorize(can_write: Callable[[], bool] | None) -> None:
    if can_write is None or can_write() is not True:
        raise PermissionError("Application permission required to write a local goal")


class GoalThread:
    """An exclusive native lease, valid only in the task that opened it."""

    def __init__(self, native: Any, can_write: Callable[[], bool] | None) -> None:
        self._thread_id: str = native.thread_id
        self._native = native
        self._can_write = can_write
        self._owner = asyncio.current_task()

    @property
    def thread_id(self) -> str:
        """Save this ID with the profile to reopen the same root."""
        return self._thread_id

    def _check(self) -> None:
        if self._owner is None or self._owner is not asyncio.current_task():
            raise RuntimeError("Local goal thread has no current owner in this task")

    async def get(self) -> CodexGoal | None:
        """Return the stored goal or None for a root without one."""
        self._check()
        raw: str = await await_session_mutation(self._native.get())
        return None if raw == "null" else CodexGoal.model_validate_json(raw)

    async def set_paused(self, objective: str, *, token_budget: int | None = None) -> CodexGoal:
        """Set a paused objective. An omitted budget preserves an existing grant.

        Use 1–4,000 characters after trimming and a positive signed 64-bit budget.
        A new goal uses the profile ceiling, if any. A budget-limited goal
        may stay limited. Inspect the returned status. After a storage error
        or cancellation, read the goal before considering another write.
        """
        self._check()
        if token_budget is not None and (
            type(token_budget) is not int or not 0 < token_budget < 2**63
        ):
            raise ValueError("Expected a positive signed 64-bit token budget")
        _authorize(self._can_write)
        raw: str = await await_session_mutation(self._native.set_paused(objective, token_budget))
        return CodexGoal.model_validate_json(raw)


@asynccontextmanager
async def open_goal(
    profile: Path,
    *,
    thread_id: str | None,
    can_write: Callable[[], bool] | None = None,
) -> AsyncIterator[GoalThread]:
    """Open a dedicated local goal root under an exclusive native writer lease.

    Supply an existing, private absolute profile path and an authorized ID.
    Pass None explicitly to create a root. Creation and each edit require
    can_write() to return True. Only the opening task may use the handle.
    Use a wheel with native Codex support. No operation starts a model turn.
    Runner uses separate ephemeral threads and does not charge this budget.

    Save the profile and ID in your application before the first edit.
    Authorize reads there too. Local history can retain previous objectives.
    Even reads acquire a writer lock and may initialize storage resources.
    Profile limits load on open. Reopen after a policy change. An edit that
    retains a grant above the new ceiling requires an explicit smaller budget.

    ```python
    from agents.models.codex_goals import open_goal

    async def create(profile, can_edit, remember):
        async with open_goal(profile, thread_id=None, can_write=can_edit) as root:
            remember(root.thread_id)
            return await root.set_paused("Review the draft", token_budget=1000)

    async def display(profile, saved_id):
        async with open_goal(profile, thread_id=saved_id) as root:
            return await root.get()
    ```

    A missing or busy root raises an error. A started write can finish even
    if the caller cancels. Creation can leave an empty root, and an edit can
    persist intent before a failed write or change the goal before a failed
    receipt. No automatic retries.
    """
    await asyncio.to_thread(_check_profile, profile)
    if thread_id is None:
        _authorize(can_write)
    try:
        native = import_module("agents.models._codex_native")
    except ModuleNotFoundError as error:
        if error.name != "agents.models._codex_native":
            raise
        raise UserError("Local goals require an SDK wheel with Codex support.") from None

    failure: BaseException | None = None
    opening = native.open_goal(profile, thread_id)

    # A cancellation traceback in the joiner must never retain the lease.
    async def wait_for_open() -> None:
        await opening

    try:
        await await_session_mutation(wait_for_open())
        thread = GoalThread(opening.result(), can_write)
        try:
            yield thread
        finally:
            thread._owner = None
            thread._can_write = None
            thread._native = None
    except BaseException as exc:
        failure = exc
        raise
    finally:
        del can_write
        if not opening.cancelled() and opening.exception() is None:
            try:
                await await_session_mutation(opening.result().close())
            except asyncio.CancelledError:
                if not isinstance(failure, asyncio.CancelledError):
                    raise
            except Exception:
                if failure is None:
                    raise
            finally:
                del opening
