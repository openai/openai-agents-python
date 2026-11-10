"""Application-owned projection for ag-ui-protocol==0.1.9; see README.md.

Keep the shared examples/codex/_ui_projection.py helper with this recipe.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from ag_ui.core import (
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder

from agents import NativeError, Session, Turn, TurnStoppedError
from agents.observation import (
    EventGap,
    OutputTextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from examples.codex._ui_projection import _complete_display


async def stream_turn(
    turn: Turn[Any], *, session: Session[Any, Any], output_type: object
) -> AsyncGenerator[str, None]:
    """Show one logical Turn through completion, or report missing history.

    The caller owns the Session and its worker. Closing this response releases
    only the passive reader. Each call starts a new UI view, never a replay.
    Pass the actual output declaration selected for this Turn: the submit
    override, or the opening Agent's output_type when no override was used.
    Pass the open owning Session to authorize a recorded-item identity check
    in plain-text mode. Never render or replay history content.
    """
    if session.id != turn.session_id:
        raise ValueError("Pass this Turn's owning Session")
    events = turn.events()
    encoder = EventEncoder()
    thread_id = turn.child_id if turn.child_id is not None else turn.session_id
    # SDK submission IDs distinguish the exact issued handle, even if a
    # later turn uses the same short Codex turn ID. They confer no authority.
    run_id = turn.submission_id
    opened: dict[str, list[str]] = {}
    started: str | None = None
    observed_turns: set[str] = set()
    saw_gap = False
    child_cancelled = False
    terminal: RunFinishedEvent | RunErrorEvent = RunErrorEvent(
        message="Observations are incomplete. Reload the authorized history view.",
        code="resync_required",
    )
    try:
        yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=run_id))
        async for event in events:
            if isinstance(event, TurnStarted) and (
                started or event.submission_id == turn.submission_id
            ):
                started = event.turn_id
                observed_turns.add(event.turn_id)
                continue
            if isinstance(event, EventGap) and not started:
                saw_gap = True
                # Ignore earlier loss only if this exact TurnStarted is still
                # retained. Never display a suffix when its start was lost.
                continue
            if not started or isinstance(event, EventGap):
                break
            if isinstance(event, OutputTextDelta):
                # This protocol forbids empty TEXT_MESSAGE_CONTENT deltas.
                if not event.delta:
                    continue
                if event.item_id not in opened:
                    opened[event.item_id] = []
                    yield encoder.encode(TextMessageStartEvent(message_id=event.item_id))
                opened[event.item_id].append(event.delta)
                yield encoder.encode(
                    TextMessageContentEvent(message_id=event.item_id, delta=event.delta)
                )
            elif isinstance(event, TurnFinished) and turn.child_id is not None:
                child_cancelled = event.status == "cancelled"
            elif isinstance(event, (ToolStarted, ToolFinished)):
                progress: dict[str, str | None] = {
                    "type": event.type,
                    "call_id": event.call_id,
                    "name": event.name,
                    "namespace": event.namespace,
                    "family": event.family,
                }
                if isinstance(event, ToolFinished):
                    progress["status"] = event.status
                yield encoder.encode(CustomEvent(name="sdk.tool_progress", value=progress))
        else:
            # An unassigned ID after stream exhaustion means no SDK start
            # for this Turn was published. A global gap cannot have hidden it.
            if started or not saw_gap or turn.turn_id is None:
                try:
                    completed = await turn.get_result()
                except TurnStoppedError:
                    terminal = RunErrorEvent(
                        message="The turn ended with a stop-related error.", code="turn_cancelled"
                    )
                except Exception as error:
                    if child_cancelled and isinstance(error, NativeError):
                        terminal = RunErrorEvent(
                            message="The turn ended with a stop-related error.",
                            code="turn_cancelled",
                        )
                    else:
                        terminal = RunErrorEvent(message="The turn failed.", code="turn_failed")
                else:
                    if started and await _complete_display(
                        session,
                        turn.child_id,
                        observed_turns,
                        started,
                        opened,
                        output_type is None or output_type is str,
                        completed.final_output,
                    ):
                        terminal = RunFinishedEvent(thread_id=thread_id, run_id=run_id)
        # No native item-end is exposed. Close only text items actually shown,
        # including on gap/error; do not claim that their text is complete.
        for item_id in opened:
            yield encoder.encode(TextMessageEndEvent(message_id=item_id))
        yield encoder.encode(terminal)
    finally:
        await events.aclose()
