from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from ..items import TResponseStreamEvent
from ..memory.session import await_session_mutation
from ..stream_events import ApprovedText, RunCompleted
from .plugin_events import notify_stream

if TYPE_CHECKING:
    from ..models.interface import Model
    from ..result import RunResultStreaming

Approve = Callable[[str], str | None | Awaitable[str | None]]
_LIMIT = 65_536


class ApprovedOutput:
    """One passive subscription to a single Runner producer."""

    def __init__(self, approve: Approve | None):
        self.approve = approve
        self.submission_id = uuid4().hex
        self.session_id: str | None = None
        self.accepted = False
        self.ready = asyncio.Event()
        self._retired: asyncio.Future[None] | None = None
        self.error: str | None = None
        self.deadline: float | None = None
        self.turn = 0
        self.turn_id = ""
        self.sequence = 0
        self.item: tuple[str, int] | None = None
        self.content_index = 0
        self.deltas: deque[str] = deque()
        self.size = 0

    def interrupt_preview(self) -> None:
        self.ready.set()
        if self._retired is not None:
            self._retired.cancel()

    def _fail(self, message: str) -> None:
        self.error = message
        self.interrupt_preview()

    def _reset(self) -> None:
        self.interrupt_preview()
        # A new deque also invalidates approvals still awaiting the application.
        self.deltas.clear()
        self.deltas = deque()
        self.size = 0

    def retire(self) -> None:
        self.turn_id = ""
        self.item = None
        self._reset()

    def for_turn(self, turn: int, model: Model) -> ApprovedOutput | None:
        if self.approve is None or self.error:
            return None
        from ..extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

        if isinstance(model, OpenAIHostedMultiAgentModel):
            self._fail("Hosted multi-agent streams do not isolate root text.")
            return None
        self.turn = turn
        return self

    def begin(self, deadline: float | None) -> None:
        self.deadline = deadline
        self.sequence = 0
        self.retire()

    def feed(self, event: TResponseStreamEvent) -> None:
        if self.approve is None or self.error:
            return
        self.ready.set()
        if getattr(event, "sequence_number", None) != self.sequence:
            self._fail("Gap in the output stream.")
            return
        self.sequence += 1
        if event.type == "response.created":
            if self.sequence != 1:
                self._fail("Unexpected response within one attempt.")
                return
            self.turn_id = event.response.id
        elif event.type in (
            "response.completed",
            "response.failed",
            "response.incomplete",
            "error",
            "response.error",
        ):
            # Retire drafts before cleanup or turn resolution can replace the candidate.
            self.retire()
        elif event.type == "response.output_item.added":
            self.item = (
                (event.item.id, event.output_index)
                if event.item.type == "message" and event.item.role == "assistant"
                else None
            )
            self.content_index = 0
            self._reset()
        elif event.type == "response.output_item.done":
            if event.item.type == "message" and self.item == (event.item.id, event.output_index):
                self.item = None
                self._reset()
        elif event.type == "response.content_part.added":
            if self.item == (event.item_id, event.output_index):
                self.content_index = event.content_index
                self._reset()
        elif event.type == "response.output_text.delta":
            if (
                not self.turn_id
                or not event.item_id
                or self.item != (event.item_id, event.output_index)
                or event.content_index != self.content_index
            ):
                self._fail("Text delta without an owned output item.")
                return
            if not event.delta:
                return
            remaining = _LIMIT - self.size
            if len(event.delta) <= remaining:
                try:
                    size = len(event.delta.encode("utf-8"))
                except UnicodeEncodeError:
                    self._fail("Output text must be valid Unicode.")
                    return
                if size <= remaining:
                    self.size += size
                    self.deltas.append(event.delta)
                    return
            self._fail("Pending output exceeds 65536 UTF-8 bytes.")

    async def read(
        self, result: RunResultStreaming
    ) -> AsyncGenerator[ApprovedText | RunCompleted, None]:
        task = result.run_loop_task
        assert task is not None

        def revoked() -> bool:
            return (
                result._cancel_mode != "none"
                or (result.is_complete and not self.accepted)
                or result._stored_exception is not None
                or result._triggered_input_guardrail_result is not None
                or bool(result.interruptions)
                or bool(getattr(task, "cancelling", lambda: 0)())
                or task.cancelled()
                or (task.done() and task.exception() is not None)
            )

        def expired() -> bool:
            return self.deadline is not None and asyncio.get_running_loop().time() >= self.deadline

        pending = ""
        deltas = self.deltas

        def preview_retired() -> bool:
            return (
                revoked()
                or expired()
                or self.error is not None
                or self.deltas is not deltas
                or result.current_turn != self.turn
            )

        try:
            while True:
                if self.error:
                    raise ValueError(self.error)
                if revoked():
                    return
                if self.deltas is not deltas:
                    deltas = self.deltas
                    pending = ""
                if not deltas or result.current_turn != self.turn or expired():
                    if task.done():
                        break
                    self.ready.clear()
                    await self.ready.wait()
                    continue
                assert self.approve is not None and self.item is not None
                pending += deltas.popleft()
                prefix = self.approve(pending)
                if inspect.isawaitable(prefix):
                    retired = self._retired = asyncio.get_running_loop().create_future()
                    owned = not asyncio.isfuture(prefix)
                    preview = asyncio.ensure_future(prefix)
                    try:
                        if not revoked():
                            await asyncio.wait(
                                (preview, retired), return_when=asyncio.FIRST_COMPLETED
                            )
                        if revoked() or retired.done():
                            continue
                        prefix = preview.result()
                    finally:
                        self._retired = None
                        # Only cancel work scheduled by this reader.
                        if owned and not preview.done():
                            preview.cancel()
                            await await_session_mutation(
                                asyncio.gather(preview, return_exceptions=True)
                            )
                        elif preview.done() and not preview.cancelled():
                            preview.exception()
                if preview_retired():
                    continue
                if prefix is None:
                    continue
                if not isinstance(prefix, str) or not pending.startswith(prefix):
                    raise ValueError("Approval must be an exact text prefix or None.")
                if prefix:
                    self.size -= len(prefix.encode("utf-8"))
                    pending = pending[len(prefix) :]
                    event = ApprovedText(
                        self.submission_id,
                        self.session_id,
                        self.turn,
                        self.turn_id,
                        self.item[0],
                        self.content_index,
                        prefix,
                    )
                    await notify_stream(result, event, preview_retired)
                    if not preview_retired():
                        yield event

            if self.accepted:
                await result._await_model_provider_cleanup()
                result._check_errors()
                if not revoked():
                    receipt = RunCompleted(self.submission_id, self.session_id)
                    # The owning reader checks the drain before publishing.
                    if result._display_driver is None:
                        await notify_stream(result, receipt, revoked)
                    if not revoked():
                        yield receipt
        finally:
            self.approve = None
            self.retire()
            result._approved_output = None
