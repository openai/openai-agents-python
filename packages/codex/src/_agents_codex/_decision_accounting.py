"""Publish auxiliary completions through the existing native usage path."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._lifecycle import _join
from .decisions import DecisionResponse
from .usage import TokenUsage

if TYPE_CHECKING:
    from .session import Session, _CallbackState


@dataclass
class _Pending:
    thread: str
    turn: str
    state: _CallbackState
    delivered: asyncio.Future[None]
    incarnation: str | None = None
    native_instance: int | None = None


def _metadata(response: DecisionResponse) -> str:
    usage = (
        {
            name: getattr(response.usage, name)
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
        }
        if isinstance(response.usage, TokenUsage)
        else None
    )
    if usage is not None and (
        any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in usage.values())
        or usage["cached_input_tokens"] > usage["input_tokens"]
        or usage["cache_write_input_tokens"] > usage["input_tokens"]
        or usage["reasoning_output_tokens"] > usage["output_tokens"]
        or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
    ):
        usage = None
    return json.dumps(
        {
            "usage": usage,
            "request_id": response.request_id if isinstance(response.request_id, str) else None,
            "model": response.model if isinstance(response.model, str) else None,
        },
        allow_nan=False,
    )


async def begin_decision_response(
    session: Session[Any, Any], state: _CallbackState, message: dict[str, Any]
) -> Callable[[DecisionResponse], Awaitable[None]]:
    """Capture the exact native owner before the provider can suspend."""
    from .session import NativeError, _ChildCallbacks

    captured = await session._native._capture_decision(json.dumps(message))
    thread, turn = captured.thread_id, captured.turn_id
    incarnation: str | None
    native_instance: int | None
    if isinstance(state, _ChildCallbacks):
        expected_thread = state.handle.child_id
        incarnation, native_instance = state.handle.instance_id, state.native_instance
    else:
        expected_thread = session.id
        incarnation, native_instance = None, None
    if state.handle is None or thread != expected_thread or turn != state.handle.turn_id:
        raise NativeError("Decision capture belongs to another callback")
    used = False

    async def record(response: DecisionResponse) -> None:
        nonlocal used
        if used:
            raise RuntimeError("Decision response was already recorded")
        used = True
        pending = _Pending(
            thread,
            turn,
            state,
            asyncio.get_running_loop().create_future(),
            incarnation,
            native_instance,
        )
        identity = captured.completion_id
        session._decision_completions[identity] = pending

        async def commit() -> None:
            error = await captured.commit(_metadata(response))
            # A successful native write can precede the Session's reader. Keep
            # the callback owned until budget accounting has consumed its event.
            await asyncio.wait(
                (pending.delivered, session._reader), return_when=asyncio.FIRST_COMPLETED
            )
            if not pending.delivered.done():
                raise NativeError("Decision completion event was not delivered")
            pending.delivered.result()
            if error is not None:
                raise NativeError(error)

        try:
            await _join(asyncio.create_task(commit()))
        finally:
            session._decision_completions.pop(identity, None)
            if pending.delivered.done() and not pending.delivered.cancelled():
                pending.delivered.exception()

    return record


def consume_decision_response(session: Session[Any, Any], event: dict[str, Any]) -> bool:
    """Consume only our registered completion and charge its captured root once."""
    message = event.get("msg", {})
    if message.get("type") != "raw_response_completed":
        return False
    pending = session._decision_completions.get(message.get("response_id"))
    if pending is None:
        return False
    if pending.delivered.done():
        return True
    try:
        if event.get("id") != pending.turn:
            raise RuntimeError("Decision completion belongs to another callback")
        if pending.incarnation is None:
            if pending.thread != session.id or any(
                event.get(key) is not None
                for key in ("subagent_id", "subagent_incarnation", "subagent_instance_id")
            ):
                raise RuntimeError("Decision completion belongs to another callback")
        elif (
            event.get("subagent_id") != pending.thread
            or event.get("subagent_incarnation") != pending.incarnation
            or pending.native_instance is not None
            and event.get("subagent_instance_id") != pending.native_instance
        ):
            raise RuntimeError("Decision completion belongs to another callback")
        budget = getattr(pending.state, "budget", None)
        if budget is not None:
            budget.observe(message.get("token_usage"))
    except BaseException as error:
        pending.delivered.set_exception(error)
    else:
        pending.delivered.set_result(None)
    return True
