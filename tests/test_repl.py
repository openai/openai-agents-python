import asyncio
import contextlib
import os
import sys
import threading
from collections.abc import Iterator

import pytest

import agents.repl as repl_module
from agents import Agent, run_demo_loop
from agents.testing import ScriptedModel

from .test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)


@pytest.mark.asyncio
async def test_run_demo_loop_conversation(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("hello")], [get_text_message("good")]])

    agent = Agent(name="test", model=model)

    inputs = iter(["Hi", "How are you?", "quit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert "good" in output
    assert model.calls[-1].input == [
        get_text_input_item("Hi"),
        get_text_message("hello").model_dump(exclude_unset=True),
        get_text_input_item("How are you?"),
    ]


@pytest.mark.asyncio
async def test_run_demo_loop_streaming(monkeypatch, capsys):
    model = ScriptedModel()
    target_agent = Agent(name="target", model=model)
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
        handoffs=[target_agent],
    )

    # A single user turn that exercises every streamed event branch:
    # a tool call, the tool output, a handoff (agent update), then a text answer.
    model.extend(
        [
            [get_function_tool_call("foo", "{}")],
            [get_handoff_tool_call(target_agent)],
            [get_text_message("all done")],
        ]
    )

    inputs = iter(["Hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=True)

    output = capsys.readouterr().out
    assert "all done" in output
    assert "[tool called]" in output
    assert "[tool output: tool_result]" in output
    assert "[Agent updated: target]" in output


@pytest.mark.asyncio
async def test_run_demo_loop_exits_on_eof(monkeypatch, capsys):
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    def raise_eof(_=" > ") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    await run_demo_loop(agent, stream=False)

    # The loop should terminate cleanly without ever invoking the model.
    assert not model.calls


@pytest.mark.asyncio
async def test_run_demo_loop_skips_empty_input(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("hello")]])
    agent = Agent(name="test", model=model)

    # Empty lines are ignored; only the non-empty input reaches the runner.
    inputs = iter(["", "Hi", "quit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert model.calls[-1].input == [get_text_input_item("Hi")]


requires_pollable_stdin = pytest.mark.skipif(
    sys.platform == "win32",
    reason="stdin cannot be waited on here, so the demo loop reads on the event loop",
)


@contextlib.contextmanager
def _pollable_stdin(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Point sys.stdin at a real pipe so the loop takes its interruptible reader path."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "r")
    monkeypatch.setattr(sys, "stdin", reader)
    try:
        yield write_fd
    finally:
        os.close(write_fd)
        reader.close()


def test_stdin_without_a_pollable_descriptor_reads_on_the_event_loop():
    """pytest's captured stdin has no descriptor, so no reader thread may be started."""
    assert repl_module._stdin_poll_fd() is None


@requires_pollable_stdin
@pytest.mark.asyncio
async def test_run_demo_loop_does_not_block_the_event_loop(monkeypatch):
    """The prompt must not hold the loop while it waits for a keystroke."""
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    released = threading.Event()

    def blocking_input(*args: object) -> str:
        # Returns only after a coroutine scheduled alongside the loop has run, so reading
        # the prompt on the event loop instead of a worker thread deadlocks here.
        if not released.wait(timeout=5):
            raise AssertionError("the event loop was blocked while waiting for input")
        raise EOFError

    monkeypatch.setattr("builtins.input", blocking_input)

    async def release_prompt() -> None:
        released.set()

    with _pollable_stdin(monkeypatch) as write_fd:
        os.write(write_fd, b"anything\n")
        await asyncio.gather(run_demo_loop(agent, stream=False), release_prompt())

    assert not model.calls


@requires_pollable_stdin
@pytest.mark.asyncio
async def test_cancelling_the_demo_loop_stops_the_stdin_reader(monkeypatch):
    """Ctrl+C must not leave a worker sitting on stdin after the loop is done.

    ``asyncio.to_thread`` cannot interrupt its worker, and the default executor is joined
    while the loop shuts down, so a reader still blocked on stdin would hang the process
    until the user pressed Enter.
    """
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    entered = threading.Event()
    exited = threading.Event()
    read_line_when_ready = repl_module._read_line_when_ready

    def tracked_reader(fd: int, stop: threading.Event) -> str | None:
        entered.set()
        try:
            return read_line_when_ready(fd, stop)
        finally:
            exited.set()

    monkeypatch.setattr(repl_module, "_read_line_when_ready", tracked_reader)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("stdin had no input"))

    # No bytes are written, so the reader stays in its poll loop until it is asked to stop.
    with _pollable_stdin(monkeypatch):
        task = asyncio.create_task(run_demo_loop(agent, stream=False))
        assert await asyncio.to_thread(entered.wait, 5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    # The reader returned before the coroutine finished, so nothing is left on stdin.
    assert exited.is_set()
    assert not model.calls


@requires_pollable_stdin
@pytest.mark.asyncio
async def test_cancelling_the_demo_loop_settles_within_one_poll_interval(monkeypatch):
    """The wait for the reader is bounded, so cancellation stays responsive."""
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    reading = threading.Event()
    read_line_when_ready = repl_module._read_line_when_ready

    def tracked_reader(fd: int, stop: threading.Event) -> str | None:
        reading.set()
        return read_line_when_ready(fd, stop)

    monkeypatch.setattr(repl_module, "_read_line_when_ready", tracked_reader)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("stdin had no input"))

    with _pollable_stdin(monkeypatch):
        task = asyncio.create_task(run_demo_loop(agent, stream=False))
        assert await asyncio.to_thread(reading.wait, 5)

        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = asyncio.get_running_loop().time() - started

    # One poll interval plus scheduling slack, nowhere near a blocked-until-Enter read.
    assert elapsed < repl_module._STDIN_POLL_INTERVAL_SECONDS * 10
