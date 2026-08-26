import asyncio
import contextlib
import os
import shutil
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

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


class StubStdinReader:
    def __init__(self, inputs: Iterator[str | BaseException]) -> None:
        self.inputs = inputs
        self.closed = False

    async def readline(self, _prompt: str) -> str:
        value = next(self.inputs)
        if isinstance(value, BaseException):
            raise value
        return value

    async def aclose(self) -> None:
        self.closed = True


def patch_stdin_reader(
    monkeypatch: pytest.MonkeyPatch, inputs: list[str | BaseException]
) -> StubStdinReader:
    reader = StubStdinReader(iter(inputs))
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)
    return reader


@pytest.mark.asyncio
async def test_run_demo_loop_conversation(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("hello")], [get_text_message("good")]])

    agent = Agent(name="test", model=model)

    reader = patch_stdin_reader(monkeypatch, ["Hi", "How are you?", "quit"])

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert "good" in output
    assert model.calls[-1].input == [
        get_text_input_item("Hi"),
        get_text_message("hello").model_dump(exclude_unset=True),
        get_text_input_item("How are you?"),
    ]
    assert reader.closed


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

    patch_stdin_reader(monkeypatch, ["Hello", "exit"])

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

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        await run_demo_loop(agent, stream=False)

    # The loop should terminate cleanly without ever invoking the model.
    assert not model.calls


@pytest.mark.asyncio
async def test_run_demo_loop_skips_empty_input(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("hello")]])
    agent = Agent(name="test", model=model)

    # Empty lines are ignored; only the non-empty input reaches the runner.
    patch_stdin_reader(monkeypatch, ["", "Hi", "quit"])

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert model.calls[-1].input == [get_text_input_item("Hi")]


@pytest.mark.asyncio
async def test_run_demo_loop_skips_whitespace_only_input(monkeypatch, capsys):
    model = ScriptedModel()
    agent = Agent(name="test", model=model)
    patch_stdin_reader(monkeypatch, ["   ", "quit"])

    await run_demo_loop(agent, stream=False)

    assert not model.calls


@pytest.mark.asyncio
async def test_run_demo_loop_does_not_block_the_event_loop(monkeypatch):
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        os.write(write_fd, b"qu")

        async def release_prompt() -> None:
            await asyncio.sleep(0)
            os.write(write_fd, b"it\n")

        try:
            await asyncio.gather(run_demo_loop(agent, stream=False), release_prompt())
        finally:
            os.close(write_fd)

    assert not model.calls


@pytest.mark.asyncio
async def test_prompt_is_shown_after_input_helper_is_ready(monkeypatch, capsys):
    model = ScriptedModel()
    agent = Agent(name="test", model=model)
    read_fd, write_fd = os.pipe()
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    real_spawn_process = repl_module._StdinReader._spawn_process

    async def spawn_process(self, prompt, encoding, errors):
        spawn_started.set()
        await release_spawn.wait()
        return await real_spawn_process(self, prompt, encoding, errors)

    monkeypatch.setattr(repl_module._StdinReader, "_spawn_process", spawn_process)
    os.write(write_fd, b"quit\n")
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        task = asyncio.create_task(run_demo_loop(agent, stream=False))
        await asyncio.wait_for(spawn_started.wait(), timeout=5)
        assert capsys.readouterr().out == ""
        release_spawn.set()
        await asyncio.wait_for(task, timeout=5)

    assert capsys.readouterr().out == " > "
    assert not model.calls


@pytest.mark.asyncio
async def test_run_demo_loop_handles_crlf_input(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("ok")]])
    agent = Agent(name="test", model=model)

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"hello\r\nquit\r\n")
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8", newline=None) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        await run_demo_loop(agent, stream=False)

    assert model.calls[-1].input == [get_text_input_item("hello")]
    assert capsys.readouterr().out.count(" > ") == 2


@pytest.mark.asyncio
async def test_run_demo_loop_works_without_asyncio_subprocess_transport(monkeypatch):
    model = ScriptedModel()
    agent = Agent(name="test", model=model)
    read_fd, write_fd = os.pipe()

    async def fail_create_subprocess_exec(*args, **kwargs):
        raise AssertionError("The blocking subprocess path must not use the event loop transport.")

    async def spawn_blocking_process(self, _prompt, encoding, errors):
        return await self._spawn_blocking_process(encoding, errors)

    monkeypatch.setattr(repl_module._StdinReader, "_spawn_process", spawn_blocking_process)
    monkeypatch.setattr(repl_module.asyncio, "create_subprocess_exec", fail_create_subprocess_exec)
    os.write(write_fd, b"quit\n")
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        await run_demo_loop(agent, stream=False)

    assert not model.calls


@pytest.mark.asyncio
async def test_blocking_subprocess_preserves_preloaded_lines(monkeypatch):
    model = ScriptedModel()
    model.extend([[get_text_message("first response")], [get_text_message("second response")]])
    agent = Agent(name="test", model=model)
    read_fd, write_fd = os.pipe()

    async def spawn_blocking_process(self, _prompt, encoding, errors):
        return await self._spawn_blocking_process(encoding, errors)

    monkeypatch.setattr(repl_module._StdinReader, "_spawn_process", spawn_blocking_process)
    os.write(write_fd, b"first\nsecond\nquit\n")
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        await run_demo_loop(agent, stream=False)

    assert len(model.calls) == 2
    assert model.calls[0].input == [get_text_input_item("first")]
    assert model.calls[1].input[-1] == get_text_input_item("second")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT behavior is under test")
@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.asyncio
async def test_run_demo_loop_ctrl_c_does_not_wait_for_stdin_worker(composed: bool):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path

    entrypoint = (
        "async def main():\n"
        "    task = asyncio.create_task(\n"
        "        run_demo_loop(Agent(name='test'), stream=False)\n"
        "    )\n"
        "    await task\n"
        "asyncio.run(main())\n"
        if composed
        else "asyncio.run(run_demo_loop(Agent(name='test'), stream=False))\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import asyncio\n"
            "from agents import Agent, run_demo_loop\n"
            f"{entrypoint}"
            "print('RETURNED', flush=True)\n"
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdin is not None

    try:
        prompt = await asyncio.wait_for(process.stdout.readexactly(3), timeout=5)
        assert prompt == b" > "

        process.send_signal(signal.SIGINT)
        await asyncio.wait_for(process.wait(), timeout=5)
        stdout = await process.stdout.read()
        stderr = await process.stderr.read()
    finally:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert process.returncode == 0
    assert stdout == b"\nRETURNED\n"
    assert stderr == b""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT behavior is under test")
@pytest.mark.parametrize("raises_keyboard_interrupt", [False, True])
@pytest.mark.asyncio
async def test_run_demo_loop_preserves_custom_sigint_handler(raises_keyboard_interrupt: bool):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path

    handler_body = "    os.write(1, b'HANDLED\\n')\n"
    if raises_keyboard_interrupt:
        handler_body += "    raise KeyboardInterrupt\n"

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import asyncio, os, signal\n"
            "from agents import Agent, run_demo_loop\n"
            "def handle_sigint(signum, frame):\n"
            f"{handler_body}"
            "signal.signal(signal.SIGINT, handle_sigint)\n"
            "asyncio.run(run_demo_loop(Agent(name='test'), stream=False))\n"
            "print('RETURNED', flush=True)\n"
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdin is not None

    try:
        prompt = await asyncio.wait_for(process.stdout.readexactly(3), timeout=5)
        assert prompt == b" > "

        process.send_signal(signal.SIGINT)
        handled = await asyncio.wait_for(process.stdout.readline(), timeout=5)
        assert handled == b"HANDLED\n"
        if not raises_keyboard_interrupt:
            assert process.returncode is None
            process.stdin.write(b"quit\n")
            await process.stdin.drain()
        await asyncio.wait_for(process.wait(), timeout=5)
        stdout = await process.stdout.read()
        stderr = await process.stderr.read()
    finally:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert process.returncode == 0
    assert stdout == (b"\nRETURNED\n" if raises_keyboard_interrupt else b"RETURNED\n")
    assert stderr == b""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal behavior is under test")
@pytest.mark.asyncio
async def test_run_demo_loop_preserves_readline_history():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path
    expect = shutil.which("expect")
    if expect is None:
        pytest.skip("expect is required for the POSIX readline regression")
    child_code = (
        "import asyncio, readline\n"
        "from agents import Agent, run_demo_loop\n"
        "from agents.testing import ScriptedModel\n"
        "from tests.test_responses import get_text_message\n"
        "model = ScriptedModel()\n"
        "model.extend([[get_text_message('ok')]])\n"
        "readline.add_history('quit')\n"
        "asyncio.run(run_demo_loop(Agent(name='test', model=model), stream=False))\n"
        "print('CALLS=' + str(len(model.calls)), flush=True)\n"
    )
    expect_script = (
        "log_user 1\n"
        "set timeout 5\n"
        f"spawn {{{sys.executable}}} -c {{{child_code}}}\n"
        'expect " > "\n'
        'send "\\033\\[A"\n'
        'expect "quit"\n'
        'send "\\r"\n'
        "expect eof\n"
    )
    process = await asyncio.create_subprocess_exec(
        expect,
        "-c",
        expect_script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

    assert process.returncode == 0, stderr.decode()
    assert b"CALLS=0" in stdout
    assert b"^[[A" not in stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal behavior is under test")
@pytest.mark.asyncio
async def test_run_demo_loop_reads_terminal_without_readline():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path
    expect = shutil.which("expect")
    if expect is None:
        pytest.skip("expect is required for the POSIX terminal regression")
    child_code = (
        "import asyncio, sys\n"
        "from agents import Agent, run_demo_loop\n"
        "assert 'readline' not in sys.modules\n"
        "asyncio.run(run_demo_loop(Agent(name='test'), stream=False))\n"
        "print('RETURNED', flush=True)\n"
    )
    expect_script = (
        "log_user 1\n"
        "set timeout 5\n"
        f"spawn {{{sys.executable}}} -c {{{child_code}}}\n"
        "expect {\n"
        '    " > " {}\n'
        "    timeout { exit 2 }\n"
        "    eof { exit 3 }\n"
        "}\n"
        'send "quit\\r"\n'
        "expect {\n"
        '    "RETURNED" {}\n'
        "    timeout { exit 4 }\n"
        "    eof { exit 5 }\n"
        "}\n"
        "expect eof\n"
    )
    process = await asyncio.create_subprocess_exec(
        expect,
        "-c",
        expect_script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

    assert process.returncode == 0, (stdout + stderr).decode()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readline history is under test")
@pytest.mark.asyncio
async def test_run_demo_loop_updates_parent_readline_history():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path
    expect = shutil.which("expect")
    if expect is None:
        pytest.skip("expect is required for the POSIX readline regression")
    child_code = (
        "import asyncio, readline\n"
        "from agents import Agent, run_demo_loop\n"
        "readline.clear_history()\n"
        "asyncio.run(run_demo_loop(Agent(name='test'), stream=False))\n"
        "history = [readline.get_history_item(index) for index in "
        "range(1, readline.get_current_history_length() + 1)]\n"
        "print('HISTORY=' + repr(history), flush=True)\n"
    )
    expect_script = (
        "log_user 1\n"
        "set timeout 5\n"
        f"spawn {{{sys.executable}}} -c {{{child_code}}}\n"
        'expect " > "\n'
        'send "\\r"\n'
        'expect " > "\n'
        'send "   \\r"\n'
        'expect " > "\n'
        'send "   \\r"\n'
        'expect " > "\n'
        'send "quit\\r"\n'
        "expect eof\n"
    )
    process = await asyncio.create_subprocess_exec(
        expect,
        "-c",
        expect_script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

    assert process.returncode == 0, stderr.decode()
    assert b"HISTORY=['   ', 'quit']" in stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal SIGINT is under test")
@pytest.mark.asyncio
async def test_run_demo_loop_ctrl_c_reaps_terminal_input_helper():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path
    expect = shutil.which("expect")
    if expect is None:
        pytest.skip("expect is required for the POSIX terminal regression")
    child_code = (
        "import asyncio\n"
        "from agents import Agent, run_demo_loop\n"
        "asyncio.run(run_demo_loop(Agent(name='test'), stream=False))\n"
    )
    expect_script = (
        "log_user 1\n"
        "set timeout 5\n"
        f"spawn {{{sys.executable}}} -c {{{child_code}}}\n"
        'expect " > "\n'
        'send "\\003"\n'
        "expect eof\n"
    )
    process = await asyncio.create_subprocess_exec(
        expect,
        "-c",
        expect_script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

    assert process.returncode == 0, stderr.decode()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal cleanup is under test")
@pytest.mark.asyncio
async def test_run_demo_loop_cancellation_restores_terminal_mode():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path
    expect = shutil.which("expect")
    if expect is None:
        pytest.skip("expect is required for the POSIX terminal regression")
    child_code = (
        "import asyncio, readline, sys, termios\n"
        "from agents import Agent, run_demo_loop\n"
        "async def main():\n"
        "    before = termios.tcgetattr(sys.stdin.fileno())\n"
        "    task = asyncio.create_task(run_demo_loop(Agent(name='test'), stream=False))\n"
        "    while termios.tcgetattr(sys.stdin.fileno()) == before:\n"
        "        await asyncio.sleep(0)\n"
        "    task.cancel()\n"
        "    try:\n"
        "        await task\n"
        "    except asyncio.CancelledError:\n"
        "        pass\n"
        "    assert termios.tcgetattr(sys.stdin.fileno()) == before\n"
        "    print('TERMINAL_RESTORED', flush=True)\n"
        "asyncio.run(main())\n"
    )
    expect_script = (
        "log_user 1\n"
        "set timeout 5\n"
        f"spawn {{{sys.executable}}} -c {{{child_code}}}\n"
        'expect "TERMINAL_RESTORED"\n'
        "expect eof\n"
    )
    process = await asyncio.create_subprocess_exec(
        expect,
        "-c",
        expect_script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

    assert process.returncode == 0, stderr.decode()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cancellation is under test")
@pytest.mark.asyncio
async def test_run_demo_loop_ctrl_c_interrupts_continuously_readable_stdin():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    if existing_path := env.get("PYTHONPATH"):
        python_path = os.pathsep.join([python_path, existing_path])
    env["PYTHONPATH"] = python_path

    with open("/dev/zero", "rb") as stdin:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import asyncio\n"
                "from agents import Agent, run_demo_loop\n"
                "asyncio.run(run_demo_loop(Agent(name='test'), stream=False))\n"
            ),
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        try:
            prompt = await asyncio.wait_for(process.stdout.readexactly(3), timeout=5)
            assert prompt == b" > "

            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    assert process.returncode == 0


@pytest.mark.parametrize("blocking_subprocess", [False, True])
@pytest.mark.asyncio
async def test_stdin_helper_is_reaped_on_cancellation(monkeypatch, capsys, blocking_subprocess):
    read_fd, write_fd = os.pipe()
    process_created = asyncio.Event()
    created_process: Any = None
    real_spawn_process = repl_module._StdinReader._spawn_process

    async def spawn_process(self, prompt, encoding, errors):
        nonlocal created_process
        if blocking_subprocess:
            created_process = await self._spawn_blocking_process(encoding, errors)
        else:
            created_process = await real_spawn_process(self, prompt, encoding, errors)
        process_created.set()
        return created_process

    monkeypatch.setattr(repl_module._StdinReader, "_spawn_process", spawn_process)

    with os.fdopen(read_fd, encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        agent = Agent(name="test", model=ScriptedModel())
        read_task = asyncio.create_task(run_demo_loop(agent, stream=False))
        try:
            await asyncio.wait_for(process_created.wait(), timeout=5)
            read_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(read_task, timeout=5)
        finally:
            os.close(write_fd)

    assert created_process is not None
    assert created_process.returncode is not None


@pytest.mark.asyncio
async def test_handled_sigint_does_not_mask_later_prompt_cancellation(monkeypatch):
    model_started = asyncio.Event()
    release_model = asyncio.Event()
    prompt_pending = asyncio.Event()
    signal_seen = False

    class PromptReader(repl_module._StdinReader):
        def __init__(self) -> None:
            super().__init__(sys.stdin)
            self.calls = 0

        async def _readline(self, _prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return "hello"
            prompt_pending.set()
            await asyncio.Future()
            raise AssertionError("The pending prompt should be cancelled.")

        async def aclose(self) -> None:
            self._restore_sigint_handler()

    class FakeRunner:
        @staticmethod
        async def run(agent, input_items, context=None, max_turns=None):
            model_started.set()
            await release_model.wait()
            return type(
                "Result",
                (),
                {
                    "final_output": None,
                    "last_agent": agent,
                    "to_input_list": lambda self: input_items,
                },
            )()

    def handle_sigint(_signum, _frame) -> None:
        nonlocal signal_seen
        signal_seen = True

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    reader = PromptReader()
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)
    monkeypatch.setattr(repl_module, "Runner", FakeRunner)

    try:
        task = asyncio.create_task(
            run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
        )
        await asyncio.wait_for(model_started.wait(), timeout=5)
        current_handler = signal.getsignal(signal.SIGINT)
        assert callable(current_handler)
        current_handler(signal.SIGINT, None)
        release_model.set()
        await asyncio.wait_for(prompt_pending.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reader._restore_sigint_handler()
        signal.signal(signal.SIGINT, previous_handler)

    assert signal_seen


@pytest.mark.asyncio
async def test_programmatic_cancellation_wins_over_overlapping_sigint(monkeypatch):
    prompt_pending = asyncio.Event()
    signal_seen = False

    class PromptReader(repl_module._StdinReader):
        async def _readline(self, _prompt: str) -> str:
            prompt_pending.set()
            await asyncio.Future()
            raise AssertionError("The pending prompt should be cancelled.")

        async def aclose(self) -> None:
            self._restore_sigint_handler()

    def handle_sigint(_signum, _frame) -> None:
        nonlocal signal_seen
        signal_seen = True

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    reader = PromptReader(sys.stdin)
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)

    try:
        task = asyncio.create_task(
            run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
        )
        await asyncio.wait_for(prompt_pending.wait(), timeout=5)
        task.cancel("programmatic")
        current_handler = signal.getsignal(signal.SIGINT)
        assert callable(current_handler)
        current_handler(signal.SIGINT, None)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reader._restore_sigint_handler()
        signal.signal(signal.SIGINT, previous_handler)

    assert signal_seen


@pytest.mark.asyncio
async def test_cancellation_during_prompt_sigint_cleanup_is_propagated(monkeypatch):
    prompt_pending = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class PromptReader(repl_module._StdinReader):
        async def _readline(self, _prompt: str) -> str:
            prompt_pending.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
            raise AssertionError("The interrupted prompt should not return.")

        async def aclose(self) -> None:
            self._restore_sigint_handler()

    reader = PromptReader(sys.stdin)
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)

    try:
        task = asyncio.create_task(
            run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
        )
        await asyncio.wait_for(prompt_pending.wait(), timeout=5)
        current_handler = signal.getsignal(signal.SIGINT)
        assert callable(current_handler)
        current_handler(signal.SIGINT, None)
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        task.cancel("programmatic")
        release_cleanup.set()

        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_cleanup.set()
        reader._restore_sigint_handler()

    assert task.cancelled()


@pytest.mark.asyncio
async def test_input_error_does_not_replace_concurrent_cancellation(monkeypatch):
    class EOFReader(repl_module._StdinReader):
        def __init__(self) -> None:
            super().__init__(sys.stdin)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def _readline(self, _prompt: str) -> str:
            self.started.set()
            await self.release.wait()
            raise EOFError

        async def aclose(self) -> None:
            self._restore_sigint_handler()

    reader = EOFReader()
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)
    task = asyncio.create_task(
        run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
    )
    await reader.started.wait()

    reader.release.set()
    task.cancel("caller cancellation")

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_handled_prompt_sigint_leaves_no_task_cancellation(monkeypatch):
    prompt_pending = asyncio.Event()

    class PromptReader(repl_module._StdinReader):
        async def _readline(self, _prompt: str) -> str:
            prompt_pending.set()
            await asyncio.Future()
            raise AssertionError("The pending prompt should be interrupted.")

        async def aclose(self) -> None:
            self._restore_sigint_handler()

    reader = PromptReader(sys.stdin)
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)

    try:
        task = asyncio.create_task(
            run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
        )
        await asyncio.wait_for(prompt_pending.wait(), timeout=5)
        current_handler = signal.getsignal(signal.SIGINT)
        assert callable(current_handler)
        current_handler(signal.SIGINT, None)
        await task
    finally:
        reader._restore_sigint_handler()

    assert not task.cancelled()
    cancelling = getattr(task, "cancelling", None)
    if cancelling is not None:
        assert cancelling() == 0


_CLEANUP_PROCESS_KILLED = -1


class _CleanupProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = _CLEANUP_PROCESS_KILLED

    async def wait(self) -> int:
        self.waiting.set()
        await self.release.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_cancellation_during_stdin_cleanup_is_propagated(monkeypatch):
    class QuittingReader(repl_module._StdinReader):
        async def readline(self, _prompt: str) -> str:
            return "quit"

    process = _CleanupProcess()
    reader = QuittingReader(sys.stdin)
    reader._process = cast(Any, process)
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)

    task = asyncio.create_task(
        run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
    )
    await asyncio.wait_for(process.waiting.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.returncode == _CLEANUP_PROCESS_KILLED


@pytest.mark.asyncio
async def test_cancellation_during_stdin_cleanup_preserves_body_error(monkeypatch):
    class FailingReader(repl_module._StdinReader):
        async def readline(self, _prompt: str) -> str:
            raise RuntimeError("body failed")

    process = _CleanupProcess()
    reader = FailingReader(sys.stdin)
    reader._process = cast(Any, process)
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)

    task = asyncio.create_task(
        run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)
    )
    await asyncio.wait_for(process.waiting.wait(), timeout=5)
    task.cancel("cleanup cancellation")
    await asyncio.sleep(0)
    process.release.set()

    with pytest.raises(RuntimeError, match="body failed"):
        await task

    assert process.killed
    assert process.returncode == _CLEANUP_PROCESS_KILLED


@pytest.mark.asyncio
async def test_caller_error_does_not_mask_cancellation_during_stdin_cleanup(monkeypatch):
    class QuittingReader(repl_module._StdinReader):
        async def readline(self, _prompt: str) -> str:
            return "quit"

    process = _CleanupProcess()
    reader = QuittingReader(sys.stdin)
    reader._process = cast(Any, process)
    monkeypatch.setattr(repl_module, "_create_stdin_reader", lambda: reader)

    async def run_with_active_caller_error() -> None:
        try:
            raise LookupError("caller error")
        except LookupError:
            await run_demo_loop(Agent(name="test", model=ScriptedModel()), stream=False)

    task = asyncio.create_task(run_with_active_caller_error())
    await asyncio.wait_for(process.waiting.wait(), timeout=5)
    task.cancel("cleanup cancellation")
    await asyncio.sleep(0)
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.returncode == _CLEANUP_PROCESS_KILLED
