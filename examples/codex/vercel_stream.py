"""Packaged Vercel UI streaming and application-owned response recipes.

Matching source builds have been providing agents.ui.vercel_stream. The source
setup in packages/codex/MAINTAINING.md#build-from-this-source has been covering
installation with the matching native extension. A framework-neutral response
body using the packaged adapter:

    from collections.abc import AsyncGenerator
    from contextlib import aclosing

    from agents import Agent
    from agents.ui import vercel_stream

    async def packaged_body(
        agent: Agent[None, str], prompt: str
    ) -> AsyncGenerator[bytes, None]:
        async with agent.session() as session:
            turn = await session.submit(prompt)
            async with aclosing(vercel_stream(turn, session=session)) as body:
                async for chunk in body:
                    yield chunk

The application has been setting Content-Type: text/event-stream and
x-vercel-ai-ui-message-stream: v1 on its HTTP response. This adapter has been
accepting plain-text output, using the Agent's default output or str without
prompted or structured overrides. The application has been owning authentication,
history projection, and the Session lifetime. In this example the response has
been owning the Session; closing the response body has been closing the Session
and stopping its work. For work surviving disconnects, a worker has been keeping
the Session open and passing its Turn to vercel_stream. Closing that iterator has
been releasing only its reader.

The adapter has been omitting tool arguments and results by default. The explicit
include_tool_data=True option has been sending retained function/MCP input and
output after settlement. The application has been deciding whether the client
has permission to receive that data. Each stream has been starting a fresh UI
message; missing observations have been producing resync_required for recovery
through an application-authorized history view.

The custom stream_turn and response_body recipes below have been illustrating
separate ownership and projection choices with the sibling _ui_projection.py
helper. The AG-UI recipe in ag_ui/README.md has been covering
AG-UI events. Client tests have been checking the custom Vercel recipe with
ai@6.0.315 and synthetic fetch, without browser rendering or real HTTP transport.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from typing import Any, TypeVar

from agents import Agent, Completed, Session, Turn, TurnStoppedError
from agents.observation import (
    EventGap,
    OutputTextDelta,
    ToolFinished,
    ToolStarted,
    TurnStarted,
)
from examples.codex._ui_projection import _complete_display

OutputT = TypeVar("OutputT")


def _sse(chunk: dict[str, Any]) -> bytes:
    return (
        "data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode()


async def stream_turn(
    turn: Turn[str], *, session: Session[Any, Any]
) -> AsyncGenerator[bytes, None]:
    """Project this Turn's retained events into a fresh UI message stream.

    The caller must submit this Turn with plain str output, without an output
    override or PromptedOutput (even PromptedOutput(str)). This is a text-only
    recipe. Turn[str] alone does not prove plain output at runtime: a Turn has
    no public output-mode getter. Check the owning Agent before submission as
    response_body does. Never pass a typed or prompted Turn to this reader.

    Text part IDs come from native output items. data-tool-progress is custom
    transient metadata, not Vercel tool input/output or an approval request.
    This recipe always starts a fresh message; it cannot resume a UI transcript.
    A Session-global gap before this exact TurnStarted does not lose this
    message. Even from the start, this Turn's observations may be lost. An
    application receiving resync_required must use its own explicit history
    projection; this recipe does not reconstruct messages from Session.items().
    A second native start also requires resync: one fresh UI message cannot
    represent an owned steering boundary. The underlying Turn keeps running.
    Closing or cancelling this iterator releases its reader only. The owner of
    the Turn or Session decides whether to stop work.

    An abort frame projects a TurnStoppedError result. A callback can forward
    that exception from another stopped Turn, including its existing cause.
    This projection does not establish who requested a stop or which Turn
    was originally stopped. It does not infer causal ownership.

    Pass the open owning Session to authorize a recorded-item identity check.
    History is used only to verify text already emitted from Turn events.
    No history content is rendered or replayed.
    """
    if session.id != turn.session_id:
        raise ValueError("Pass this Turn's owning Session")
    events = turn.events()
    # Observations do not promise a delta for a done-only response. Retain
    # shown text by real item ID so success cannot silently omit the result.
    opened: dict[str, list[str]] = {}
    started: str | None = None
    saw_gap = False
    try:
        yield _sse({"type": "start"})
        async for event in events:
            if (
                isinstance(event, TurnStarted)
                and started is None
                and event.submission_id == turn.submission_id
            ):
                started = event.turn_id
                continue
            if isinstance(event, EventGap) and not started:
                saw_gap = True
                # All earlier loss is irrelevant only if this turn's start
                # remains available. Never display a suffix without it.
                continue
            if not started or isinstance(event, (EventGap, TurnStarted)):
                for item_id in opened:
                    yield _sse({"type": "text-end", "id": item_id})
                yield _sse({"type": "error", "errorText": "resync_required"})
                yield b"data: [DONE]\n\n"
                return
            if isinstance(event, OutputTextDelta):
                if event.item_id not in opened:
                    opened[event.item_id] = []
                    yield _sse({"type": "text-start", "id": event.item_id})
                opened[event.item_id].append(event.delta)
                yield _sse({"type": "text-delta", "id": event.item_id, "delta": event.delta})
            elif isinstance(event, (ToolStarted, ToolFinished)):
                progress: dict[str, Any] = {
                    "type": event.type,
                    "call_id": event.call_id,
                    "name": event.name,
                    "namespace": event.namespace,
                    "family": event.family,
                }
                if isinstance(event, ToolFinished):
                    progress["status"] = event.status
                yield _sse({"type": "data-tool-progress", "data": progress, "transient": True})
        for item_id in opened:
            yield _sse({"type": "text-end", "id": item_id})
        # After the stream ends, an unassigned ID means this Turn never
        # published an SDK start, so the gap cannot have hidden its start.
        if not started and saw_gap and turn.turn_id is not None:
            yield _sse({"type": "error", "errorText": "resync_required"})
        else:
            try:
                completed = await turn.get_result()
            except TurnStoppedError:
                yield _sse({"type": "abort"})
            except Exception:
                yield _sse({"type": "error", "errorText": "turn_failed"})
            else:
                if started and await _complete_display(
                    session, turn.child_id, {started}, started, opened, True, completed.final_output
                ):
                    yield _sse({"type": "finish"})
                else:
                    # No public item ID exists for text seen only at completion.
                    # Let the application choose its authorized history view.
                    yield _sse({"type": "error", "errorText": "resync_required"})
        yield b"data: [DONE]\n\n"
    finally:
        await events.aclose()


async def response_body(agent: Agent[Any, str], prompt: str) -> AsyncGenerator[bytes, None]:
    """Own a plain-text Session; closing the response stops that work.

    Typed and prompted output is outside this streaming recipe. Reject it before
    opening the Session, including PromptedOutput(str). Use the settled result
    directly in an application that needs structured output.

    For work that survives disconnects, let a worker own the Session, and pass
    its existing Turn to stream_turn instead. Closing stream_turn alone releases
    only its reader. Each stream starts a fresh message. If this Turn's
    bounded observations may have been lost,
    resync_required ends the stream. Earlier Turns' loss alone does not
    prevent this Turn from streaming. The application chooses what
    Session.items() history it may show; this recipe does not resume, persist
    or reconstruct a UI transcript.
    """
    if agent.output_type is not None and agent.output_type is not str:
        raise TypeError("This example requires plain-text Agent output (default or str)")
    async with agent.session() as session:
        turn = await session.submit(prompt)
        async with aclosing(stream_turn(turn, session=session)) as body:
            async for chunk in body:
                yield chunk


async def export_metadata(
    session: Session[Any, Any],
    sink: Callable[[Mapping[str, str | int]], Awaitable[None]],
) -> None:
    """The application owns and cancels/joins this task at its chosen boundary.

    A slow sink can lose observations without blocking native work. Export loss
    markers, but no text, arguments, results, credentials or interaction grants.
    """
    events = session.events()
    try:
        async for event in events:
            row: dict[str, str | int] = {
                "session_id": event.session_id,
                "sequence": event.sequence,
                "type": event.type,
            }
            if isinstance(event, EventGap):
                row["first_lost_sequence"] = event.first_sequence
            await sink(row)
    finally:
        await events.aclose()


async def passive_wait(turn: Turn[OutputT], seconds: float) -> Completed[OutputT]:
    """A wait timeout releases only the waiter; the exact Turn continues.

    The caller may await turn.get_result() again. It must still own the open Session
    for work to survive the timeout.
    """
    return await asyncio.wait_for(turn.get_result(), timeout=seconds)


async def wait_owned_turn(turn: Turn[OutputT], seconds: float) -> Completed[OutputT]:
    """Use only for a root Turn owned by this caller, with no successor policy.

    On timeout stop and join precisely this Turn, never whichever one is active
    later. For a child Turn, use Subagent.try_cancel(turn) and check its boolean
    result. If an operation owns an entire Session including automatic successors, use
    session.cancel_all() at the owning scope instead.
    """
    try:
        return await asyncio.wait_for(turn.get_result(), timeout=seconds)
    except asyncio.TimeoutError:
        await turn.cancel(mode="force")
        raise
