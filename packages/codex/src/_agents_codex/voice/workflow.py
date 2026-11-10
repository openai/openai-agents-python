"""Run speech transcriptions through a caller-owned native Session."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .._function_tools import FunctionTool, Tool
from .._public import preserve_module_identity as _preserve_module_identity
from ..observation import EventGap, OutputTextDelta
from ..session import Session, _split_tools
from ..tools import ToolRef
from ..turn import Turn


@dataclass
class _Invocation:
    submission: asyncio.Task[Turn[Any]]
    cancellation: asyncio.Task[None] | None = None
    finished: bool = False


async def _join(task: asyncio.Task[Any]) -> None:
    """Join owned cleanup even if the caller receives another cancellation."""
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            continue
    task.result()


class NativeSessionVoiceWorkflow:
    """Speak accepted native output with the Session's history and policies.

    By default, speech starts after output validation and completion acceptance.
    Set early_speech=True to speak native text deltas as they arrive. These
    provisional words may include intermediate assistant messages and may later
    fail output validation; spoken audio cannot be retracted. Tool payloads and
    provisional output-repair messages are not spoken.
    Use a text-output Session; a structured result requires a custom workflow
    that selects and renders the fields the application wants spoken.

    The application opens and closes the Session and handles its interactions.
    Each transcription submits a new root turn. Concurrent runs are rejected;
    an already busy Session follows its normal rejection policy. Close a run
    iterator explicitly when stopping early, for example with contextlib.aclosing.
    Closing or cancelling a run stops and joins only its own native turn.
    The optional on_turn callback receives that exact Turn once after admission,
    before speech. An awaitable callback is awaited. Callback failures stop and
    join the owned turn; the callback may observe or cancel that turn, but a
    concurrent run on this workflow is rejected.

    Omit tools to use the Session's current defaults for each run. An explicit
    sequence is captured at construction and limits each submitted turn; an
    empty sequence excludes all tools. Runs use the current acknowledged
    replacements for selected opening tools. An active profile that does not
    admit the selection causes submission to fail.
    """

    def __init__(
        self,
        session: Session[Any, Any],
        *,
        tools: Sequence[Tool[Any] | ToolRef] | None = None,
        early_speech: bool = False,
        on_turn: Callable[[Turn[Any]], None | Awaitable[None]] | None = None,
    ) -> None:
        if type(early_speech) is not bool:
            raise TypeError("early_speech must be a bool")
        if on_turn is not None and not callable(on_turn):
            raise TypeError("on_turn must be callable")
        self._session = session
        self._tools = tuple(tools) if tools is not None else None
        self._early_speech = early_speech
        self._on_turn = on_turn
        self._invocation: _Invocation | None = None

    async def on_start(self) -> AsyncGenerator[str, None]:
        """Start without adding a greeting to the caller's conversation."""
        return
        yield

    async def run(self, transcription: str) -> AsyncGenerator[str, None]:
        """Yield accepted final text, or explicitly opted-in provisional deltas."""
        if not isinstance(transcription, str) or not transcription.strip():
            raise ValueError("transcription must be a nonempty string")
        if self._invocation is not None:
            raise RuntimeError("NativeSessionVoiceWorkflow already has an active run")

        tools = self._tools
        if tools:
            self._session._check_open()
            source = self._session._effective_agent()
            current, _, _ = _split_tools(
                tools,
                source.programmatic_tool_calling,
                available=self._session._bindings,
                ceiling=source.tools,
                environment=self._session._executor,
                native_agents=self._session._native_agents,
                handoffs=self._session._effective_handoffs(),
            )
            # Resolve only after the original selector passes exact source checks.
            # Submission still rejects a binding superseded by a racing mutation.
            bindings = {tool.ref: tool for tool in current}
            tools = tuple(
                bindings[tool.ref] if isinstance(tool, FunctionTool) else tool for tool in tools
            )

        # Reserve before awaiting admission. A passive wait retains the exact Turn if
        # cancellation races with submit returning its locally issued handle.
        invocation = _Invocation(
            asyncio.create_task(
                self._session.submit(transcription)
                if tools is None
                else self._session.submit(transcription, tools=tools)
            )
        )
        self._invocation = invocation
        completed = False
        primary_error: BaseException | None = None
        try:
            if not invocation.submission.done():
                await asyncio.wait((invocation.submission,))
            turn = invocation.submission.result()
            if invocation.cancellation is not None:
                raise asyncio.CancelledError
            if self._on_turn is not None:
                callback_result = self._on_turn(turn)
                if inspect.isawaitable(callback_result):
                    await callback_result
                if invocation.cancellation is not None:
                    raise asyncio.CancelledError
            spoken: dict[str, list[str]] = {}
            if self._early_speech:
                events = turn.events()
                interrupted_stream = False
                try:
                    async for event in events:
                        if invocation.cancellation is not None:
                            raise asyncio.CancelledError
                        # The Turn cursor includes output-validation repair work.
                        # Only the original task's text is opted into early speech.
                        if getattr(event, "parent_submission_id", None) is not None:
                            continue
                        if isinstance(event, EventGap):
                            interrupted_stream = True
                        if isinstance(event, OutputTextDelta) and not interrupted_stream:
                            if event.delta:
                                spoken.setdefault(event.item_id, []).append(event.delta)
                                yield event.delta
                finally:
                    await events.aclose()
            result = await turn.get_result()
            if invocation.cancellation is not None:
                raise asyncio.CancelledError
            completed = True
            if not isinstance(result.final_output, str):
                raise TypeError(
                    "NativeSessionVoiceWorkflow requires a string final_output; "
                    "use a text-output Session or a custom workflow to render structured output"
                )
            text = result.final_output
            # A done-only message has its own item identity, even when it repeats
            # an earlier tool preamble. A partial stream needs its accepted suffix.
            previous = ""
            if spoken and not result.repair_attempts:
                final_item_id: str | None = None
                turn_ids = {step.turn_id for step in turn._steps()}
                async for item in self._session.items():
                    if (
                        item["type"] == "message"
                        and item["role"] == "assistant"
                        and item["turn_id"] in turn_ids
                        and item["status"] == "completed"
                    ):
                        displayed = item.get("display_text")
                        if displayed is None:
                            displayed = "".join(part.get("text", "") for part in item["content"])
                        if displayed == text:
                            final_item_id = item["id"]
                if final_item_id is not None:
                    previous = "".join(spoken.get(final_item_id, []))
            if previous and text.startswith(previous):
                text = text[len(previous) :]
            if text:
                yield text
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
