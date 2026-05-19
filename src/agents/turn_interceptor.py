from __future__ import annotations

import inspect
import queue
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .agent import Agent
from .exceptions import InputGuardrailTripwireTriggered, UserError
from .items import TResponseInputItem
from .logger import logger
from .run_context import RunContextWrapper
from .run_internal.guardrails import run_input_guardrails
from .util._types import MaybeAwaitable

InjectionRecord = tuple[str, TResponseInputItem]
"""A pair of (injection_id, item) representing a queued injection."""


class TurnActionType(str, Enum):
    PROCEED = "proceed"
    INJECT_INPUT = "inject_input"


@dataclass(frozen=True)
class TurnAction:
    action: TurnActionType
    data: list[TResponseInputItem] | None = None

    @classmethod
    def proceed(cls) -> TurnAction:
        return cls(action=TurnActionType.PROCEED)

    @classmethod
    def inject_input(cls, data: list[TResponseInputItem]) -> TurnAction:
        return cls(action=TurnActionType.INJECT_INPUT, data=data)


class TurnInterceptor:
    """Manages mid-run message injection with version-based staleness detection."""

    def __init__(
        self,
        on_consumed: Callable[[list[InjectionRecord]], MaybeAwaitable[None]] | None = None,
        on_rejected: Callable[[list[InjectionRecord]], MaybeAwaitable[None]] | None = None,
    ):
        self._queue: queue.Queue[tuple[int, str, TResponseInputItem]] = queue.Queue()
        self._current_agent: Agent[Any] | None = None
        self._context: Any = None
        self._version: int = 0
        self.on_consumed = on_consumed
        self.on_rejected = on_rejected

    # ─── Framework lifecycle methods ─────────────────────────────────────

    async def reset(self, agent: Agent[Any], context: Any) -> None:
        """Start of run. Reject stale items from previous run, then clear state."""
        await self._reject_all_pending()
        self._context = context
        self._current_agent = agent
        self._version += 1

    def update_agent(self, agent: Agent[Any]) -> None:
        """Top of each loop iteration. Bump version on agent change."""
        if agent is self._current_agent:
            return
        self._current_agent = agent
        self._version += 1

    # ─── User-facing API ─────────────────────────────────────────────────

    def inject(self, item: str | TResponseInputItem) -> str:
        """Inject a message for the next turn. Returns injection_id.

        - Accepts string (auto-wrapped as user message) or raw dict.
        - Returns immediately. Outcome delivered via on_consumed/on_rejected.
        - Guardrails run at drain time (inside the run loop).
        - Raises UserError if called before run starts.
        - Thread-safe: callable from any thread, no event loop required.
        """
        if self._current_agent is None:
            raise UserError("Cannot inject before the run has started")

        if isinstance(item, str):
            item = {"role": "user", "content": item}

        injection_id = str(uuid.uuid4())
        self._queue.put_nowait((self._version, injection_id, item))
        return injection_id

    # ─── Framework drain interface ───────────────────────────────────────

    async def __call__(self) -> TurnAction:
        """Drain queue at NextStepRunAgain. Validate and inject."""
        valid, stale = self._drain()

        await self._notify_rejected(stale)

        if not valid:
            return TurnAction.proceed()

        accepted, failed = await self._run_guardrails(valid)

        await self._notify_rejected(failed)

        if not accepted:
            return TurnAction.proceed()

        await self._notify_consumed(accepted)
        return TurnAction.inject_input([item for _, item in accepted])

    # ─── Private helpers ────────────────────────────────────────────────

    async def _run_guardrails(
        self,
        records: list[InjectionRecord],
    ) -> tuple[list[InjectionRecord], list[InjectionRecord]]:
        """Run input guardrails on each record. Returns (accepted, rejected)."""
        agent = self._current_agent
        if not agent or not agent.input_guardrails:
            return records, []

        ctx = RunContextWrapper(context=self._context)
        accepted: list[InjectionRecord] = []
        rejected: list[InjectionRecord] = []

        for injection_id, item in records:
            try:
                await run_input_guardrails(agent, agent.input_guardrails, [item], ctx)
                accepted.append((injection_id, item))
            except InputGuardrailTripwireTriggered:
                rejected.append((injection_id, item))

        return accepted, rejected

    async def _notify_rejected(self, records: list[InjectionRecord]) -> None:
        """Fire on_rejected callback if there are rejected records."""
        if records and self.on_rejected:
            await self._invoke_callback(self.on_rejected, records)

    async def _notify_consumed(self, records: list[InjectionRecord]) -> None:
        """Fire on_consumed callback if there are consumed records."""
        if records and self.on_consumed:
            await self._invoke_callback(self.on_consumed, records)

    async def _invoke_callback(
        self,
        callback: Callable[[list[InjectionRecord]], MaybeAwaitable[None]],
        records: list[InjectionRecord],
    ) -> None:
        """Invoke a callback safely. Exceptions are logged, not propagated."""
        try:
            result = callback(records)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "TurnInterceptor callback %s raised an exception", callback, exc_info=True
            )

    def _drain(self) -> tuple[list[InjectionRecord], list[InjectionRecord]]:
        """Drain queue. Split by version: valid (current) vs rejected (stale)."""
        valid: list[InjectionRecord] = []
        rejected: list[InjectionRecord] = []

        try:
            while True:
                version, injection_id, item = self._queue.get_nowait()
                if version == self._version:
                    valid.append((injection_id, item))
                else:
                    rejected.append((injection_id, item))
        except queue.Empty:
            pass

        return valid, rejected

    async def _reject_all_pending(self) -> None:
        """End of run. Reject everything remaining in queue."""
        valid, stale = self._drain()
        all_discarded = valid + stale

        if all_discarded and self.on_rejected:
            await self._invoke_callback(self.on_rejected, all_discarded)
