"""SDK declarations have been binding configured children to issued tool contexts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import TypeVar

from ._callback_execution import _call_callback
from ._compat import cancel_once
from ._function_tools import FunctionTool, ToolContext, _PythonTool, function_tool
from ._lifecycle import _close_after_error
from .agent import AgentToolStreamEvent
from .observation import AgentProfileChanged, EventGap
from .tools import _CapturedFunction

if TYPE_CHECKING:
    from .agent import Agent
    from .session import Completed, Session
    from .turn import Turn

ContextT = TypeVar("ContextT")
OutputT = TypeVar("OutputT")


_logger = logging.getLogger("agents.agent")


@dataclass(frozen=True)
class _ConfiguredAgentTool:
    agent: Agent[Any, Any]
    extractor: Callable[[Completed[Any]], str | Awaitable[str]] | None
    observer: Callable[[AgentToolStreamEvent], None | Awaitable[None]] | None
    max_pending_events: int | None


def _create_agent_tool(
    agent: Agent[ContextT, OutputT],
    tool_name: str,
    tool_description: str | None,
    extractor: Callable[[Completed[OutputT]], str | Awaitable[str]] | None,
    observer: Callable[[AgentToolStreamEvent], None | Awaitable[None]] | None,
    max_pending_events: int | None,
) -> _PythonTool[ContextT, object]:
    if observer is not None and not callable(observer):
        raise TypeError("on_stream must be callable or None")
    if max_pending_events is not None and (
        type(max_pending_events) is not int or max_pending_events <= 0
    ):
        raise ValueError("on_stream_max_pending_events must be a positive integer or None")

    async def invoke(context: ToolContext[Any], input: str) -> str:
        if context._configured_agent_invoker is None:
            raise RuntimeError("Configured children require an issued parent tool context")
        return await context._configured_agent_invoker(input)

    declaration = function_tool(
        name=tool_name,
        description=(
            tool_description if tool_description is not None else "Run the configured child Agent."
        ),
        error_function=None,
    )(invoke)
    bind = declaration._bind
    assert bind is not None
    configured = _ConfiguredAgentTool(agent, extractor, observer, max_pending_events)

    def capture(
        context: object, policy_context: object, source: FunctionTool[Any, Any, Any]
    ) -> _CapturedFunction:
        return replace(bind(context, policy_context, source), configured_agent=configured)

    object.__setattr__(declaration, "_bind", capture)
    return cast("_PythonTool[ContextT, object]", declaration)


class _AgentToolObservationOverflow(RuntimeError):
    pass


async def _observed_agent_tool_result(
    child: Session[Any, Any],
    turn: Turn[Any],
    configured: _ConfiguredAgentTool,
    context: ToolContext[Any],
    admitted: Callable[[], bool],
    active_agent: Agent[Any, Any] | None,
) -> Completed[Any]:
    observer = configured.observer
    if observer is None:
        return await turn.get_result()

    limit = configured.max_pending_events
    queued: asyncio.Queue[AgentToolStreamEvent | None] = asyncio.Queue(
        maxsize=0 if limit is None else limit + 1
    )
    cursor = turn.events()

    async def produce() -> None:
        nonlocal active_agent
        try:
            async for event in cursor:
                if isinstance(event, EventGap):
                    raise _AgentToolObservationOverflow(
                        "Configured Agent tool observation history was lost"
                    )
                if isinstance(event, AgentProfileChanged):
                    active_agent = child._original_agent_for_profile(
                        {"profile_id": event.profile_id, "epoch": event.profile_epoch}
                    )
                if limit is not None and queued.qsize() >= limit:
                    await asyncio.sleep(0)
                    if queued.qsize() >= limit:
                        raise _AgentToolObservationOverflow(
                            "Configured Agent tool on_stream backlog exceeded its limit"
                        )
                assert active_agent is not None
                queued.put_nowait(AgentToolStreamEvent(event, active_agent, context.call))
        finally:
            queued.put_nowait(None)

    async def consume() -> None:
        while (event := await queued.get()) is not None:
            try:
                await _call_callback(observer, admitted, event)
            except Exception:
                # Application observations cannot replace the native result.
                _logger.warning("Configured Agent tool on_stream callback failed")

    producer = asyncio.create_task(produce())
    consumer = asyncio.create_task(consume())

    async def retire(stop: bool = False) -> None:
        cancel_once(producer)
        cancel_once(consumer)
        try:
            if stop:
                await asyncio.gather(
                    turn.cancel(),
                    asyncio.gather(producer, consumer, return_exceptions=True),
                )
            else:
                await asyncio.gather(producer, consumer, return_exceptions=True)
        finally:
            await cursor.aclose()

    try:
        await asyncio.gather(producer, consumer)
    except BaseException as error:
        stop = isinstance(error, _AgentToolObservationOverflow)
        await _close_after_error(error, lambda: retire(stop=stop))
        raise
    await retire()
    return await turn.get_result()
