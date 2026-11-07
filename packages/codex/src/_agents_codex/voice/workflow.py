"""Run speech transcriptions through a caller-owned native Session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from .._public import preserve_module_identity as _preserve_module_identity
from ..observation import EventGap, OutputTextDelta, TurnStarted
from ..session import Session
from ..turn import Turn


class VoiceEventGapError(RuntimeError):
    """Native output observations were lost before they could be spoken."""


@dataclass
class _Invocation:
    submission: asyncio.Task[Turn[str]]
    cancellation: asyncio.Task[None] | None = None
    finished: bool = False


async def _join(task: asyncio.Task[Any]) -> None:
    """Join owned cleanup even if the caller receives another cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


class NativeSessionVoiceWorkflow:
    """Speak native output while retaining the Session's history and policies.

    The application opens and closes the Session and handles its interactions.
    Each transcription submits a new root turn. Concurrent runs are rejected;
    an already busy Session follows its normal rejection policy. Close a run
    iterator explicitly when stopping early, for example with contextlib.aclosing.
    Closing or cancelling a run stops and joins only its own native turn.
    """

    def __init__(self, session: Session[Any, Any]) -> None:
        self._session = session
        self._invocation: _Invocation | None = None

    async def on_start(self) -> AsyncGenerator[str, None]:
        """Start without adding a greeting to the caller's conversation."""
        return
        yield

    async def run(self, transcription: str) -> AsyncGenerator[str, None]:
        """Yield this transcription's native text; propagate terminal failures."""
        if not isinstance(transcription, str) or not transcription.strip():
            raise ValueError("transcription must be a nonempty string")
        if self._invocation is not None:
            raise RuntimeError("NativeSessionVoiceWorkflow already has an active run")

        # Reserve before awaiting admission. Shielding retains the exact Turn if
        # cancellation races with submit returning its locally issued handle.
        invocation = _Invocation(
            asyncio.create_task(self._session.submit(transcription, output_type=str))
        )
        self._invocation = invocation
        completed = False
        primary_error: BaseException | None = None
        try:
            turn = await asyncio.shield(invocation.submission)
            started = False
            preceding_gap = False
            async for event in turn.events():
                if invocation.cancellation is not None:
                    raise asyncio.CancelledError
                if isinstance(event, TurnStarted) and event.submission_id == turn.submission_id:
                    started = True
                    preceding_gap = False
                elif isinstance(event, EventGap):
                    if started:
                        raise VoiceEventGapError("Native voice output observations were lost")
                    preceding_gap = True
                elif isinstance(event, OutputTextDelta):
                    if preceding_gap:
                        raise VoiceEventGapError("Native voice turn start or output was lost")
                    yield event.delta
            if preceding_gap:
                raise VoiceEventGapError("Native voice turn observations were lost")
            await turn.get_result()
            if invocation.cancellation is not None:
                raise asyncio.CancelledError
            completed = True
        except BaseException as error:
            primary_error = error
            raise
        finally:
            invocation.finished = True
            try:
                if not completed:
                    await self._cancel(invocation)
            except BaseException:
                # Keep the reservation after failed cleanup. A later cancel can
                # retry the same exact turn before any new work is admitted.
                if primary_error is None:
                    raise
            else:
                if self._invocation is invocation:
                    self._invocation = None

    async def _cancel(self, invocation: _Invocation) -> None:
        cancellation = invocation.cancellation
        if cancellation is None or (
            cancellation.done()
            and not cancellation.cancelled()
            and cancellation.exception() is not None
        ):

            async def stop() -> None:
                try:
                    turn = await invocation.submission
                except Exception:
                    # Failed admission issued no handle and owns no native work.
                    return
                await turn.cancel()

            invocation.cancellation = asyncio.create_task(stop())
        assert invocation.cancellation is not None
        await _join(invocation.cancellation)

    async def cancel(self) -> None:
        """Stop and join the current owned turn without closing the Session.

        A suspended run iterator must still be closed by its consumer before a
        new run can begin. Calling cancel again is safe and joins the same stop.
        """
        invocation = self._invocation
        if invocation is not None:
            await self._cancel(invocation)
            if invocation.finished and self._invocation is invocation:
                self._invocation = None


_preserve_module_identity(globals(), "agents.voice.workflow")
