"""External Codex CLI delegation through a native function tool."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from copy import deepcopy
from typing import Any

from agents.classic.extensions.experimental.codex.codex import Codex
from agents.classic.extensions.experimental.codex.codex_options import (
    CodexOptions,
    coerce_codex_options,
)
from agents.classic.extensions.experimental.codex.events import (
    ItemCompletedEvent,
    ThreadErrorEvent,
    ThreadEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
)
from agents.classic.extensions.experimental.codex.items import is_agent_message_item
from agents.classic.extensions.experimental.codex.thread import Thread
from agents.classic.extensions.experimental.codex.thread_options import (
    ThreadOptions,
    coerce_thread_options,
)
from agents.classic.extensions.experimental.codex.turn_options import (
    TurnOptions,
    coerce_turn_options,
)

from ._function_tools import _PythonTool, function_tool
from ._public import preserve_module_identity as _preserve_module_identity


def codex_tool(
    *,
    codex: Codex | None = None,
    codex_options: CodexOptions | Mapping[str, Any] | None = None,
    thread_options: ThreadOptions | Mapping[str, Any] | None = None,
    turn_options: TurnOptions | Mapping[str, Any] | None = None,
    name: str = "codex",
    description: str = "Run an external Codex CLI task in the configured workspace.",
    persist_session: bool = False,
    on_event: Callable[[ThreadEvent], None | Awaitable[None]] | None = None,
) -> _PythonTool[Any, object]:
    """Declare external CLI delegation without starting a subprocess.

    The application authorizes CLI credentials, executable and workspace through
    CodexOptions and ThreadOptions. Native Environment grants do not constrain
    this application function. The returned tool uses the native function route;
    the CLI owns its own task and its usage is returned separately in the result.
    on_event observes the CLI stream. Python task cancellation closes and joins
    the stream's subprocess. TurnOptions supports stdout inactivity timeouts.

    persist_session reuses this tool's most recently completed thread and
    serializes its calls. Model-supplied thread_id explicitly selects a CLI
    thread; omit it to start a fresh thread or reuse the configured session.
    """
    if codex is not None and codex_options is not None:
        raise ValueError("supply codex or codex_options, not both")
    if not isinstance(persist_session, bool):
        raise TypeError("persist_session must be a bool")
    if on_event is not None and not callable(on_event):
        raise TypeError("on_event must be callable or None")
    client = codex
    captured_codex_options = deepcopy(coerce_codex_options(codex_options))
    captured_thread_options = deepcopy(coerce_thread_options(thread_options))
    turn_options = coerce_turn_options(turn_options)
    captured_turn_options = (
        TurnOptions(
            output_schema=deepcopy(turn_options.output_schema),
            signal=turn_options.signal,
            idle_timeout_seconds=turn_options.idle_timeout_seconds,
        )
        if turn_options is not None
        else None
    )
    persisted: Thread | None = None
    lock = asyncio.Lock()

    async def execute(prompt: str, thread_id: str | None) -> dict[str, Any]:
        nonlocal persisted, client
        if client is None:
            client = Codex(captured_codex_options)
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if thread_id is not None and not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        thread = (
            client.resume_thread(thread_id, captured_thread_options)
            if thread_id is not None
            else persisted
            if persist_session and persisted is not None
            else client.start_thread(captured_thread_options)
        )
        stream = await thread.run_streamed(prompt, captured_turn_options)
        response = ""
        usage = None
        async with aclosing(stream.events):
            async for event in stream.events:
                if on_event is not None:
                    observed = on_event(event)
                    if inspect.isawaitable(observed):
                        await observed
                if isinstance(event, ItemCompletedEvent) and is_agent_message_item(event.item):
                    response = event.item.text
                elif isinstance(event, TurnCompletedEvent):
                    usage = event.usage.as_dict() if event.usage is not None else None
                elif isinstance(event, TurnFailedEvent):
                    raise RuntimeError(event.error.message)
                elif isinstance(event, ThreadErrorEvent):
                    raise RuntimeError(event.message)
        if persist_session:
            persisted = thread
        return {"thread_id": thread.id, "response": response, "usage": usage}

    @function_tool(name=name, description=description)
    async def invoke(prompt: str, thread_id: str | None = None) -> dict[str, Any]:
        if persist_session:
            async with lock:
                return await execute(prompt, thread_id)
        return await execute(prompt, thread_id)

    return invoke


__all__ = ["Codex", "CodexOptions", "ThreadOptions", "TurnOptions", "codex_tool"]


_preserve_module_identity(globals(), "agents.codex")
