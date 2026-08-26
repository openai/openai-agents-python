from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import IO, Any, TextIO, cast

from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from .agent import Agent
from .items import TResponseInputItem
from .result import RunResultBase
from .run import DEFAULT_MAX_TURNS, Runner
from .run_context import TContext
from .stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent

_INPUT_PROCESS_PATH = Path(__file__).with_name("_repl_input_process.py")
_InputProcess = asyncio.subprocess.Process | subprocess.Popen[bytes]


class _StdinReader:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._terminal = sys.platform != "win32" and stream.isatty()
        self._process: _InputProcess | None = None
        self._spawn_task: asyncio.Task[_InputProcess] | None = None
        self._control_socket: socket.socket | None = None
        self._control_fd: int | None = None
        self._protocol_reader: asyncio.StreamReader | None = None
        self._blocking_protocol_task: asyncio.Task[bytes] | None = None
        self._terminal_fd: int | None = None
        self._terminal_attributes: list[Any] | None = None
        self._prompt_interrupted: asyncio.Future[None] | None = None
        self._previous_sigint_handler: Any = None
        self._sigint_handler: Any = None
        self._install_sigint_handler()

    async def readline(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        interrupted = loop.create_future()
        read_task = asyncio.create_task(self._readline(prompt))
        self._prompt_interrupted = interrupted
        outer_exception: BaseException | None = None
        try:
            done, _ = await asyncio.wait(
                (read_task, interrupted), return_when=asyncio.FIRST_COMPLETED
            )
            if interrupted in done:
                raise KeyboardInterrupt
            return read_task.result()
        except BaseException as exc:
            outer_exception = exc
            raise
        finally:
            self._prompt_interrupted = None
            interrupted.cancel()
            if not read_task.done():
                read_task.cancel()
            if outer_exception is not None:
                cancellation = await _finish_task(read_task)
                if cancellation is not None and isinstance(outer_exception, KeyboardInterrupt):
                    raise cancellation
            else:
                await read_task

    async def _readline(self, prompt: str) -> str:
        process = await self._get_process(prompt)
        if not self._terminal:
            sys.stdout.write(prompt)
            sys.stdout.flush()
        if self._control_socket is not None:
            await asyncio.get_running_loop().sock_sendall(self._control_socket, b"R")
        elif self._control_fd is not None:
            _write_all(self._control_fd, b"R")
        else:
            raise RuntimeError("The stdin helper process has no control channel.")

        if isinstance(process, subprocess.Popen):
            if process.stdout is None:
                raise RuntimeError("The stdin helper process has no output pipe.")
            self._blocking_protocol_task = asyncio.create_task(
                asyncio.to_thread(_read_blocking_response, process.stdout)
            )
        blocking_protocol_task = self._blocking_protocol_task
        protocol_reader = self._protocol_reader
        if blocking_protocol_task is None and protocol_reader is None:
            raise RuntimeError("The stdin helper process has no output pipe.")

        try:
            if blocking_protocol_task is not None:
                payload = await asyncio.shield(blocking_protocol_task)
                self._blocking_protocol_task = None
            else:
                assert protocol_reader is not None
                header = await protocol_reader.readexactly(8)
                payload_size = int.from_bytes(header, "big")
                if payload_size == 0:
                    raise RuntimeError("The stdin helper process returned an invalid response.")
                payload = await protocol_reader.readexactly(payload_size)
        except (asyncio.IncompleteReadError, EOFError):
            await _wait_for_process(process)
            raise RuntimeError(
                f"The stdin helper process exited unexpectedly with code {process.returncode}."
            ) from None

        kind, value, history = json.loads(payload)
        if kind == "eof":
            raise EOFError
        if kind == "decode_error":
            try:
                encoding, object_hex, start, end, reason = value
                error = UnicodeDecodeError(encoding, bytes.fromhex(object_hex), start, end, reason)
            except (TypeError, ValueError):
                raise RuntimeError(
                    "The stdin helper process returned an invalid response."
                ) from None
            raise error
        if kind == "error":
            raise RuntimeError(f"The stdin helper process failed with {value}.")
        if kind != "line" or not isinstance(value, str):
            raise RuntimeError("The stdin helper process returned an invalid response.")
        if self._terminal:
            if not isinstance(history, list) or not all(isinstance(item, str) for item in history):
                raise RuntimeError("The stdin helper process returned an invalid response.")
            for item in history:
                _record_readline_history(item)
        return value

    async def aclose(self) -> None:
        cancellation: asyncio.CancelledError | None = None
        process = self._process
        if process is None and self._spawn_task is not None:
            process, cancellation = await _finish_spawn(self._spawn_task)
            self._spawn_task = None
            self._process = process

        if process is None:
            self._restore_terminal()
            self._restore_sigint_handler()
            self._close_control()
            if cancellation is not None:
                raise cancellation
            return

        try:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            wait_cancellation = await _wait_for_process(process)
            if cancellation is None:
                cancellation = wait_cancellation
        finally:
            self._restore_terminal()
            self._restore_sigint_handler()
            self._process = None
            self._protocol_reader = None
            blocking_protocol_task = self._blocking_protocol_task
            self._blocking_protocol_task = None
            if blocking_protocol_task is not None:
                task_cancellation = await _finish_task(blocking_protocol_task)
                if cancellation is None:
                    cancellation = task_cancellation
            if isinstance(process, subprocess.Popen) and process.stdout is not None:
                process.stdout.close()
            self._close_control()
        if cancellation is not None:
            raise cancellation

    async def _get_process(self, prompt: str) -> _InputProcess:
        if self._process is not None:
            return self._process

        if self._spawn_task is None:
            encoding = self._stream.encoding
            if not encoding:
                raise RuntimeError("The stdin stream has no text encoding.")
            self._spawn_task = asyncio.create_task(
                self._spawn_process(prompt, encoding, self._stream.errors or "strict")
            )

        process = await asyncio.shield(self._spawn_task)
        self._spawn_task = None
        self._process = process
        return process

    async def _spawn_process(self, prompt: str, encoding: str, errors: str) -> _InputProcess:
        if sys.platform == "win32":
            return await self._spawn_blocking_process(encoding, errors)

        parent_socket, child_socket = socket.socketpair()
        parent_socket.setblocking(False)
        if self._terminal:
            import termios

            self._terminal_fd = self._stream.fileno()
            self._terminal_attributes = termios.tcgetattr(self._terminal_fd)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                str(_INPUT_PROCESS_PATH),
                encoding,
                errors,
                "terminal" if self._terminal else "stream",
                f"fd:{child_socket.fileno()}",
                prompt,
                stdin=self._stream,
                stdout=None if self._terminal else asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if self._terminal else None,
                pass_fds=(child_socket.fileno(),),
            )
        except BaseException:
            parent_socket.close()
            raise
        finally:
            child_socket.close()

        self._control_socket = parent_socket
        self._protocol_reader = process.stderr if self._terminal else process.stdout
        config = json.dumps(
            _readline_config() if self._terminal else {"readline": False, "history": []}
        ).encode()
        try:
            await asyncio.get_running_loop().sock_sendall(
                parent_socket, len(config).to_bytes(8, "big") + config
            )
        except BaseException:
            parent_socket.close()
            self._control_socket = None
            self._protocol_reader = None
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await _wait_for_process(process)
            raise
        return process

    async def _spawn_blocking_process(self, encoding: str, errors: str) -> subprocess.Popen[bytes]:
        control_read_fd, control_write_fd = os.pipe()
        control_handle: int | None = None
        try:
            popen_kwargs: dict[str, Any]
            if sys.platform == "win32":
                import msvcrt

                control_handle = msvcrt.get_osfhandle(control_read_fd)
                cast(Any, os).set_handle_inheritable(control_handle, True)
                startup_info = subprocess.STARTUPINFO()
                startup_info.lpAttributeList = {"handle_list": [control_handle]}
                control_argument = f"handle:{control_handle}"
                popen_kwargs = {"close_fds": True, "startupinfo": startup_info}
            else:
                control_argument = f"fd:{control_read_fd}"
                popen_kwargs = {"pass_fds": (control_read_fd,)}

            process = cast(
                subprocess.Popen[bytes],
                await asyncio.to_thread(
                    cast(Any, subprocess.Popen),
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        str(_INPUT_PROCESS_PATH),
                        encoding,
                        errors,
                        "stream",
                        control_argument,
                    ],
                    stdin=self._stream,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    **popen_kwargs,
                ),
            )
        except BaseException:
            os.close(control_write_fd)
            raise
        finally:
            if control_handle is not None:
                with contextlib.suppress(OSError):
                    cast(Any, os).set_handle_inheritable(control_handle, False)
            os.close(control_read_fd)

        if process.stdout is None:
            os.close(control_write_fd)
            process.kill()
            await _wait_for_process(process)
            raise RuntimeError("The stdin helper process has no output pipe.")
        self._control_fd = control_write_fd
        config = json.dumps({"readline": False, "history": []}).encode()
        try:
            _write_all(control_write_fd, len(config).to_bytes(8, "big") + config)
        except BaseException:
            self._close_control()
            if process.returncode is None:
                process.kill()
            await _wait_for_process(process)
            process.stdout.close()
            raise
        return process

    def _close_control(self) -> None:
        if self._control_socket is not None:
            self._control_socket.close()
            self._control_socket = None
        if self._control_fd is not None:
            os.close(self._control_fd)
            self._control_fd = None

    def _restore_terminal(self) -> None:
        terminal_fd = self._terminal_fd
        terminal_attributes = self._terminal_attributes
        self._terminal_fd = None
        self._terminal_attributes = None
        if terminal_fd is None or terminal_attributes is None:
            return
        if sys.platform == "win32":
            return

        import termios

        with contextlib.suppress(OSError):
            termios.tcsetattr(terminal_fd, termios.TCSANOW, terminal_attributes)

    def _install_sigint_handler(self) -> None:
        previous_handler = signal.getsignal(signal.SIGINT)
        if not callable(previous_handler):
            return
        handles_default_sigint = _handles_default_asyncio_sigint(previous_handler)

        def handle_sigint(signum: int, frame: Any) -> None:
            interrupted = self._prompt_interrupted
            if handles_default_sigint and interrupted is not None and not interrupted.done():
                interrupted.get_loop().call_soon_threadsafe(_interrupt_prompt, interrupted)
                return
            try:
                previous_handler(signum, frame)
            except KeyboardInterrupt:
                if interrupted is None or interrupted.done():
                    raise
                interrupted.get_loop().call_soon_threadsafe(_interrupt_prompt, interrupted)

        try:
            signal.signal(signal.SIGINT, handle_sigint)
        except ValueError:
            return
        self._previous_sigint_handler = previous_handler
        self._sigint_handler = handle_sigint

    def _restore_sigint_handler(self) -> None:
        handler = self._sigint_handler
        previous_handler = self._previous_sigint_handler
        self._sigint_handler = None
        self._previous_sigint_handler = None
        if handler is None or signal.getsignal(signal.SIGINT) is not handler:
            return
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, previous_handler)


def _readline_config() -> dict[str, bool | list[str]]:
    readline: Any = sys.modules.get("readline")
    if readline is None:
        return {"readline": False, "history": []}

    history = [
        item
        for index in range(1, readline.get_current_history_length() + 1)
        if (item := readline.get_history_item(index)) is not None
    ]
    return {"readline": True, "history": history}


def _record_readline_history(value: str) -> None:
    readline: Any = sys.modules.get("readline")
    if readline is not None:
        readline.add_history(value)


def _interrupt_prompt(interrupted: asyncio.Future[None]) -> None:
    if not interrupted.done():
        interrupted.set_result(None)


def _handles_default_asyncio_sigint(handler: Any) -> bool:
    if handler is signal.default_int_handler:
        return True
    if not isinstance(handler, functools.partial):
        return False

    runner_type = getattr(asyncio, "Runner", None)
    runner_handler = getattr(runner_type, "_on_sigint", None)
    handler_function = handler.func
    return (
        runner_type is not None
        and getattr(handler_function, "__func__", None) is runner_handler
        and isinstance(getattr(handler_function, "__self__", None), runner_type)
    )


def _read_blocking_response(stream: IO[bytes]) -> bytes:
    header = _read_exactly(stream, 8)
    payload_size = int.from_bytes(header, "big")
    if payload_size == 0:
        raise RuntimeError("The stdin helper process returned an invalid response.")
    return _read_exactly(stream, payload_size)


def _read_exactly(stream: IO[bytes], size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, data: bytes) -> None:
    while data:
        written = os.write(descriptor, data)
        if written == 0:
            raise BrokenPipeError
        data = data[written:]


async def _finish_spawn(
    task: asyncio.Task[_InputProcess],
) -> tuple[_InputProcess | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except BaseException:
            break
    try:
        return task.result(), cancellation
    except BaseException:
        return None, cancellation


async def _wait_for_process(
    process: _InputProcess,
) -> asyncio.CancelledError | None:
    cancellation: asyncio.CancelledError | None = None
    wait_awaitable: Coroutine[Any, Any, int]
    if isinstance(process, subprocess.Popen):
        wait_awaitable = asyncio.to_thread(process.wait)
    else:
        wait_awaitable = process.wait()
    wait_task = asyncio.create_task(wait_awaitable)
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    wait_task.result()
    return cancellation


async def _finish_task(task: asyncio.Task[Any]) -> asyncio.CancelledError | None:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    with contextlib.suppress(BaseException):
        task.result()
    return cancellation


def _create_stdin_reader() -> _StdinReader:
    return _StdinReader(sys.stdin)


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

    stdin_reader = _create_stdin_reader()
    body_raised = False
    try:
        current_agent = agent
        input_items: list[TResponseInputItem] = []
        while True:
            try:
                user_input = await stdin_reader.readline(" > ")
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
    except BaseException:
        body_raised = True
        raise
    finally:
        try:
            await stdin_reader.aclose()
        except asyncio.CancelledError:
            if not body_raised:
                raise
