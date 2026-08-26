from __future__ import annotations

import asyncio
import os
import select
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import FrameType
from typing import Any

from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from .agent import Agent
from .items import TResponseInputItem
from .result import RunResultBase
from .run import DEFAULT_MAX_TURNS, Runner
from .run_context import TContext
from .stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent

_STDIN_POLL_INTERVAL_SECONDS = 0.1
"""How long a waiting reader blocks before re-checking whether it was asked to stop."""


def _interruptible_stdin_fd() -> int | None:
    """Return the stdin descriptor only if a reader waiting on it can always be stopped.

    A reader is safe to start on a worker thread only when every blocking step it takes can
    be interrupted. Waiting on the descriptor is interruptible; the ``input()`` that follows
    is not, so readiness has to already guarantee a complete line.

    Only a terminal in canonical mode gives that guarantee: it reports readiness once the
    user presses Enter. On a pipe or a regular file, ``select`` fires on the first byte, so
    a writer that sends a partial line and pauses would leave ``input()`` blocked on the
    missing newline with no way to stop it. Those cases, a console on Windows, and a
    captured or synthetic stdin with no descriptor all read on the event loop instead --
    the behavior this module had before -- rather than start a worker that cannot be
    stopped and would hang loop shutdown until stdin delivered another line.
    """
    if sys.platform == "win32":
        # select() accepts only sockets here, and the console reports readiness per keystroke
        # rather than per line, so neither gives an interruptible wait.
        return None

    import termios

    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    if not os.isatty(fd):
        return None
    try:
        mode = termios.tcgetattr(fd)
    except (termios.error, OSError, ValueError):
        return None
    local_flags = mode[3]
    if not local_flags & termios.ICANON:
        # A terminal taken out of canonical mode reports readiness per keystroke, so the
        # same partial-line problem applies.
        return None
    return fd


def _read_line_when_ready(fd: int, stop: threading.Event) -> str | None:
    """Wait for a line on ``fd`` and read it, returning None if asked to stop first.

    The wait is split into ``_STDIN_POLL_INTERVAL_SECONDS`` slices so setting ``stop`` ends
    this reader within one slice. ``fd`` comes from :func:`_interruptible_stdin_fd`, so it
    is a canonical-mode terminal and readiness means a complete line is waiting: the
    ``input()`` below consumes it instead of blocking.
    """
    while not stop.is_set():
        try:
            ready, _, _ = select.select([fd], [], [], _STDIN_POLL_INTERVAL_SECONDS)
        except (OSError, ValueError):
            # stdin stopped being selectable underneath us; treat it as end of input rather
            # than blocking in a read this reader could no longer be stopped from.
            return None
        if ready:
            return input()
    return None


@contextmanager
def _catching_sigint(stop: threading.Event) -> Iterator[Callable[[], bool]]:
    """Take SIGINT for the duration of the block, waking the reader waiting on ``stop``.

    Ctrl+C only raises KeyboardInterrupt in the thread blocked on ``input()``. With the read
    on a worker, nothing raises it there, and the interrupt reaches ``asyncio.Runner``
    instead, which cancels the REPL task: the loop never takes the clean exit its
    ``KeyboardInterrupt`` handler provides, and ``asyncio.run()`` re-raises the interrupt at
    the call site as an uncaught traceback. Handling SIGINT here keeps the task alive so the
    caller can stop the reader and raise KeyboardInterrupt itself, in the loop.

    Yields a predicate reporting whether an interrupt arrived.
    """
    interrupted = False

    def handle(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True
        # All this needs to do is end the reader; the exception is raised by the caller.
        stop.set()

    try:
        previous = signal.signal(signal.SIGINT, handle)
    except ValueError:
        # Handlers may only be installed from the main thread; elsewhere SIGINT is not ours
        # to take, so leave it alone and report no interrupt.
        yield lambda: False
        return
    try:
        yield lambda: interrupted
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)


async def _read_user_input(prompt: str) -> str:
    """Read one line from stdin without holding the event loop or leaking a reader.

    Raises:
        EOFError: If stdin reached end of input.
        KeyboardInterrupt: If a Ctrl+C arrived while waiting for the line.
    """
    fd = _interruptible_stdin_fd()
    if fd is None:
        return input(prompt)

    print(prompt, end="", flush=True)
    stop = threading.Event()
    with _catching_sigint(stop) as interrupted:
        reader = asyncio.ensure_future(asyncio.to_thread(_read_line_when_ready, fd, stop))
        try:
            line = await asyncio.shield(reader)
        except asyncio.CancelledError:
            # Cancelled from outside the REPL. The worker cannot be interrupted, and the
            # default executor is joined while the loop shuts down. Stop the reader and wait
            # for it so no worker is still sitting on stdin once this coroutine is done; the
            # poll interval bounds that wait.
            stop.set()
            with suppress(BaseException):
                await asyncio.shield(reader)
            raise

    if interrupted():
        raise KeyboardInterrupt
    if line is None:
        raise EOFError
    return line


async def run_demo_loop(
    agent: Agent[Any],
    *,
    stream: bool = True,
    context: TContext | None = None,
    max_turns: int | None = DEFAULT_MAX_TURNS,
) -> None:
    """Run a simple REPL loop with the given agent.

    This utility allows quick manual testing and debugging of an agent from the
    command line. Conversation state is preserved across turns. Enter ``exit``
    or ``quit`` to stop the loop.

    Args:
        agent: The starting agent to run.
        stream: Whether to stream the agent output.
        context: Additional context information to pass to the runner.
        max_turns: Maximum number of turns for the runner to iterate. Pass ``None`` to disable
            the turn limit.
    """

    current_agent = agent
    input_items: list[TResponseInputItem] = []
    while True:
        try:
            user_input = await _read_user_input(" > ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user_input.strip().lower() in {"exit", "quit"}:
            break
        if not user_input.strip():
            continue

        input_items.append({"role": "user", "content": user_input})

        result: RunResultBase
        if stream:
            result = Runner.run_streamed(
                current_agent, input=input_items, context=context, max_turns=max_turns
            )
            async for event in result.stream_events():
                if isinstance(event, RawResponsesStreamEvent):
                    if isinstance(event.data, ResponseTextDeltaEvent):
                        print(event.data.delta, end="", flush=True)
                elif isinstance(event, RunItemStreamEvent):
                    if event.item.type == "tool_call_item":
                        print("\n[tool called]", flush=True)
                    elif event.item.type == "tool_call_output_item":
                        print(f"\n[tool output: {event.item.output}]", flush=True)
                elif isinstance(event, AgentUpdatedStreamEvent):
                    print(f"\n[Agent updated: {event.new_agent.name}]", flush=True)
            print()
        else:
            result = await Runner.run(
                current_agent, input_items, context=context, max_turns=max_turns
            )
            if result.final_output is not None:
                print(result.final_output)

        current_agent = result.last_agent
        input_items = result.to_input_list()
