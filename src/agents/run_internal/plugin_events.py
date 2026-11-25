"""Private delivery to the active agent's plugin observers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .._redaction import current_redactor, require_clean
from ..agent_tool_state import get_agent_tool_state_scope
from ..logger import logger
from ..plugins import AgentEvent, ToolProgress
from .plugin_cancellation import cancelled, streams

if TYPE_CHECKING:
    from ..agent import Agent, AgentBase
    from ..plugins import Plugin, _EventKind
    from ..result import RunResultStreaming
    from ..run_context import RunContextWrapper
    from ..stream_events import ApprovedText, RunCompleted
    from ..tool import FunctionTool, Tool
    from ..tool_context import ToolContext


def _selection(
    context: RunContextWrapper[Any], agent: AgentBase[Any] | None
) -> tuple[Plugin[Any], ...]:
    for owner, plugins in context._plugin_selections:
        if owner is agent:
            return plugins
    return ()


async def _notify(
    callbacks: tuple[Callable[..., Any], ...],
    slot: str,
    event: ApprovedText | RunCompleted | ToolProgress | AgentEvent,
    warned: set[str],
    stopped: Callable[[], bool],
) -> None:
    if isinstance(event, AgentEvent):
        require_clean([event.agent, event.tool, event.namespace, event.target])
    for callback in callbacks:
        if stopped() or cancelled():
            return
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
        except Exception:
            pass
        if slot not in warned:
            warned.add(slot)
            logger.warning("plugin_observer_failed %s", slot)


def agent_events(
    context: RunContextWrapper[Any],
    agent: Agent[Any],
    kind: _EventKind,
    *,
    tool: Tool | None = None,
    target: Agent[Any] | None = None,
) -> tuple[Awaitable[None], ...]:
    """Join SDK hook delivery only when this agent selected lifecycle observers."""
    if not context._plugin_selections:
        return ()
    plugins = _selection(context, agent)
    callbacks = tuple(
        p.hooks.on_event for p in plugins if p.hooks is not None and p.hooks.on_event is not None
    )
    if not callbacks:
        return ()
    name = namespace = None
    if tool is not None:
        from .._tool_identity import get_explicit_function_tool_namespace
        from ..tool import FunctionTool

        if not isinstance(tool, FunctionTool) or not (
            any(t is tool for t in agent.tools) or any(t is tool for p in plugins for t in p.tools)
        ):
            return ()
        name, namespace = tool.name, get_explicit_function_tool_namespace(tool)
    event = AgentEvent(kind, agent.name, name, namespace, target.name if target else None)
    task = asyncio.current_task()

    def stopped() -> bool:
        return bool(getattr(task, "cancelling", lambda: 0)())

    return (_notify(callbacks, "on_event", event, set(), stopped),)


async def notify_stream(
    run: RunResultStreaming,
    event: ApprovedText | RunCompleted,
    stopped: Callable[[], bool],
) -> None:
    from ..stream_events import ApprovedText

    plugins = _selection(run.context_wrapper, run.current_agent)
    if not plugins:
        return
    slot = "on_preview" if isinstance(event, ApprovedText) else "on_stream_completed"
    callbacks = tuple(
        callback
        for plugin in plugins
        if plugin.hooks is not None and (callback := getattr(plugin.hooks, slot)) is not None
    )
    if callbacks:
        if run._plugin_warnings is None:
            run._plugin_warnings = set()
        await _notify(callbacks, slot, event, run._plugin_warnings, stopped)


@contextmanager
def progress_scope(context: ToolContext[Any], tool: FunctionTool) -> Iterator[None]:
    """Attach only for the duration of a function invocation."""
    plugins = _selection(context, context.agent)
    if not plugins or current_redactor() is not None:
        yield
        return
    callbacks = tuple(
        p.hooks.on_tool_progress
        for p in plugins
        if p.hooks is not None and p.hooks.on_tool_progress is not None
    )
    scope = get_agent_tool_state_scope(context)
    if not callbacks or not scope:
        yield
        return
    owner = next((p.name for p in plugins if any(t is tool for t in p.tools)), None)
    active = streams.get()
    run = active[-1]() if active else None
    if run and get_agent_tool_state_scope(run.context_wrapper) != scope:
        run = None  # An ordinary nested run may inherit the parent's task context.
    submission_id = (run._progress_submission_id if run else None) or scope
    call_id = context.tool_call_id
    task = asyncio.current_task()
    warned: set[str] = set()
    sequence = sent = 0
    pending: str | None = None
    busy = False

    def stopped() -> bool:
        return (
            context._report_progress is None
            or (run is not None and run._cancel_mode != "none")
            or bool(getattr(task, "cancelling", lambda: 0)())
        )

    async def report(message: str) -> None:
        nonlocal sequence, sent, pending, busy
        if stopped() or current_redactor() is not None:
            return
        if not isinstance(message, str) or len(message) > 4096:
            raise ValueError("Tool progress must be text of at most 4096 characters.")
        sequence += 1
        pending = message
        if busy:
            return
        busy = True
        try:
            while pending is not None and not stopped():
                event = ToolProgress(
                    submission_id, owner, call_id, sequence, sequence - sent - 1, pending
                )
                sent, pending = sequence, None
                await _notify(callbacks, "on_tool_progress", event, warned, stopped)
        finally:
            busy = False

    context._report_progress = report
    try:
        yield
    finally:
        context._report_progress = None
