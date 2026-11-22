"""Adapt strict checkpoints to the existing Session persistence pipeline."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING, Any

from ..items import ToolApprovalItem, TResponseInputItem
from ..memory.resumable_session import (
    CodexChecklist,
    ResumableSession,
    ResumableSessionError,
    _ResumableSessionBinding,
)
from ..memory.session_settings import SessionSettings
from ..stream_events import RunItemStreamEvent, StreamEvent
from .run_steps import QueueCompleteSentinel

if TYPE_CHECKING:
    from ..memory.session import Session
    from ..models._run_context import ModelToolHooks
    from ..result import RunResult, RunResultStreaming
    from .run_steps import ProcessedResponse


class ResumableRun:
    """Stage one run's session writes in memory until the store confirms its checkpoint."""

    def __init__(self, source: ResumableSession | None) -> None:
        self.session_id = source.session_id if source is not None else ""
        self.session_settings: SessionSettings | None = None
        self.codex_checklist: CodexChecklist | None = None
        self._source = source
        self._items: list[TResponseInputItem] = []

    async def begin(self, input: Any, tool_hooks: ModelToolHooks) -> None:
        if self._source is None:
            raise ResumableSessionError("invalid_state")
        await self._source._begin_run(input, tool_hooks)
        self._items = self._source._get_history()

    def bind_state(self, state: Any) -> None:
        """Attach the current store claim to the internal execution state."""
        assert self._source is not None
        state._resumable_binding = _ResumableSessionBinding(
            self._source, self._source._checkpoint.revision
        )

    async def finish(self, result: RunResult | RunResultStreaming) -> None:
        assert self._source is not None
        state = result.to_state() if result.interruptions else None
        if state is not None:
            state._resumable_binding = "detached"
        result._resumable_binding = await self._source._save_checkpoint(
            self._items, state, codex_checklist=self.codex_checklist
        )

    def configure_stream(self, result: RunResultStreaming) -> None:
        queue = _CheckpointEventQueue()
        result._event_queue = queue

        async def save(snapshot: RunResultStreaming) -> None:
            await self.finish(snapshot)
            queue.publish()

        result._resumable_checkpoint = save

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            return copy.deepcopy(self._items)
        return copy.deepcopy(self._items[-limit:]) if limit > 0 else []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self._items.extend(copy.deepcopy(items))

    async def pop_item(self) -> TResponseInputItem | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


class _CheckpointEventQueue(asyncio.Queue[StreamEvent | QueueCompleteSentinel]):
    """Hold semantic run items until the external checkpoint acknowledges them."""

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[StreamEvent] = []
        self._published = False

    def put_nowait(self, item: StreamEvent | QueueCompleteSentinel) -> None:
        actionable = isinstance(item, RunItemStreamEvent) and (
            item.name == "message_output_created" or isinstance(item.item, ToolApprovalItem)
        )
        if (
            not self._published
            and (self._pending or actionable)
            and not isinstance(item, QueueCompleteSentinel)
        ):
            self._pending.append(item)
        else:
            super().put_nowait(item)

    def publish(self) -> None:
        self._published = True
        for item in self._pending:
            super().put_nowait(item)
        self._pending.clear()


def prepare_run_session(session: Session | ResumableSession | None, input: Any) -> Session | None:
    from ..run_state import RunState

    binding = input._resumable_binding if isinstance(input, RunState) else None
    if binding is not None:
        if isinstance(binding, _ResumableSessionBinding):
            if session is not None and session is not binding.session:
                return ResumableRun(None)
            return ResumableRun(binding.session)
        # Reject a detached state in begin() so streamed errors use the redacted path.
        return ResumableRun(None)
    if isinstance(session, ResumableSession):
        return ResumableRun(session)
    return session


async def checkpoint_streamed_result(
    result: RunResultStreaming,
    *,
    interruptions: list[ToolApprovalItem] | None = None,
    processed_response: ProcessedResponse | None = None,
) -> None:
    callback = result._resumable_checkpoint
    if callback is not None:
        snapshot = copy.copy(result)
        snapshot._resumable_checkpoint = None
        if interruptions is not None:
            snapshot.interruptions = interruptions
            snapshot._last_processed_response = processed_response
        await callback(snapshot)
        result._resumable_binding = snapshot._resumable_binding
        result._resumable_checkpoint = None
