from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.capabilities import Shell, ShellToolSet
from agents.sandbox.capabilities.tools import (
    ExecCommandArgs,
    ExecCommandTool,
    WriteStdinArgs,
    WriteStdinTool,
)
from agents.sandbox.errors import ExecTimeoutError, ExecTransportError, PtySessionNotFoundError
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.pty_types import PtyExecUpdate
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, User
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from tests.utils.factories import TestSessionState


class _ShellSession(BaseSandboxSession):
    def __init__(self, manifest: Manifest) -> None:
        self.state = TestSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.exec_calls: list[tuple[str, float | None, bool | list[str]]] = []
        self.exec_users: list[str | None] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def running(self) -> bool:
        return True

    async def read(self, path: Path, *, user: object = None) -> io.BytesIO:
        _ = (path, user)
        raise AssertionError("read() should not be called")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called")

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = command
        _ = timeout
        raise AssertionError("_exec_internal() should not be called directly")

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        user: str | User | None = None,
        shell: bool | list[str] = False,
    ) -> ExecResult:
        self.exec_users.append(user.name if isinstance(user, User) else user)
        rendered_command = " ".join(str(part) for part in command)
        self.exec_calls.append((rendered_command, timeout, shell))
        return ExecResult(
            stdout=f"stdout: {rendered_command}".encode(),
            stderr=f"stderr: {rendered_command}".encode(),
            exit_code=7,
        )

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data


class _TimeoutShellSession(_ShellSession):
    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        user: str | User | None = None,
        shell: bool | list[str] = False,
    ) -> ExecResult:
        _ = (command, user, shell)
        raise ExecTimeoutError(command=("sleep 30",), timeout_s=timeout)


class _OutputShellSession(_ShellSession):
    def __init__(
        self,
        manifest: Manifest,
        *,
        stdout: bytes,
        stderr: bytes,
        exit_code: int = 7,
    ) -> None:
        super().__init__(manifest)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        user: str | User | None = None,
        shell: bool | list[str] = False,
    ) -> ExecResult:
        self.exec_users.append(user.name if isinstance(user, User) else user)
        rendered_command = " ".join(str(part) for part in command)
        self.exec_calls.append((rendered_command, timeout, shell))
        return ExecResult(stdout=self.stdout, stderr=self.stderr, exit_code=self.exit_code)


class _PtyShellSession(_ShellSession):
    def __init__(self, manifest: Manifest) -> None:
        super().__init__(manifest)
        self._next_session_id = 1337
        self._live_sessions: set[int] = set()
        self.last_exec_yield_time_s: float | None = None
        self.last_exec_user: str | None = None
        self.last_write_yield_time_s: float | None = None

    def supports_pty(self) -> bool:
        return True

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (command, timeout, shell, tty, max_output_tokens)
        self.last_exec_user = user.name if isinstance(user, User) else user
        self.last_exec_yield_time_s = yield_time_s
        session_id = self._next_session_id
        self._next_session_id += 1
        self._live_sessions.add(session_id)
        return PtyExecUpdate(
            process_id=session_id,
            output=b"",
            exit_code=None,
            original_token_count=None,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = max_output_tokens
        self.last_write_yield_time_s = yield_time_s
        if session_id not in self._live_sessions:
            raise PtySessionNotFoundError(session_id=session_id)

        self._live_sessions.discard(session_id)
        return PtyExecUpdate(
            process_id=None,
            output=chars.encode("utf-8", errors="replace"),
            exit_code=0,
            original_token_count=None,
        )


class _PtyNoStdinShellSession(_PtyShellSession):
    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (chars, yield_time_s, max_output_tokens)
        if session_id not in self._live_sessions:
            raise PtySessionNotFoundError(session_id=session_id)
        raise RuntimeError("stdin is not available for this process")


class _PtyTransportFailingShellSession(_OutputShellSession):
    def __init__(
        self,
        manifest: Manifest,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        transport_context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(manifest, stdout=stdout, stderr=stderr, exit_code=exit_code)
        self.transport_context = transport_context or {}
        self.exec_call_count = 0

    def supports_pty(self) -> bool:
        return True

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        user: str | User | None = None,
        shell: bool | list[str] = False,
    ) -> ExecResult:
        self.exec_call_count += 1
        return await super().exec(*command, timeout=timeout, user=user, shell=shell)

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (timeout, shell, user, tty, yield_time_s, max_output_tokens)
        raise ExecTransportError(
            command=command,
            context=self.transport_context,
            cause=RuntimeError("connection closed while reading HTTP status line"),
        )


def _patch_shell_tool_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunk_id: str,
    start: float,
    end: float,
) -> None:
    monkeypatch.setattr(
        "agents.sandbox.capabilities.tools.shell_tool.uuid.uuid4",
        lambda: uuid.UUID(chunk_id),
    )
    times = iter([start, end])
    monkeypatch.setattr(
        "agents.sandbox.capabilities.tools.shell_tool.time.perf_counter",
        lambda: next(times),
    )


class TestShellCapability:
    def test_tools_requires_bound_session(self) -> None:
        capability = Shell()

        with pytest.raises(ValueError, match="Shell capability is not bound to a SandboxSession"):
            capability.tools()

    def test_tools_exposes_exec_command_function_tool_after_bind(self) -> None:
        capability = Shell()
        capability.bind(_ShellSession(Manifest(root="/workspace")))

        tools = capability.tools()

        assert len(tools) == 1
        assert isinstance(tools[0], ExecCommandTool)
        assert isinstance(tools[0], FunctionTool)
        assert tools[0].name == "exec_command"

    def test_tools_exposes_write_stdin_for_pty_sessions(self) -> None:
        capability = Shell()
        capability.bind(_PtyShellSession(Manifest(root="/workspace")))

        tools = capability.tools()

        assert len(tools) == 2
        assert isinstance(tools[0], ExecCommandTool)
        assert isinstance(tools[1], WriteStdinTool)
        assert tools[0].name == "exec_command"
        assert tools[1].name == "write_stdin"

    def test_configure_tools_can_customize_shell_approvals_after_clone(self) -> None:
        async def exec_command_needs_approval(
            _ctx: Any, params: dict[str, Any], _call_id: str
        ) -> bool:
            return str(params["cmd"]).startswith("rm ")

        async def write_stdin_needs_approval(
            _ctx: Any, params: dict[str, Any], _call_id: str
        ) -> bool:
            return str(params["chars"]) == "\u0003"

        def configure_tools(toolset: ShellToolSet) -> None:
            toolset.exec_command.needs_approval = exec_command_needs_approval
            assert toolset.write_stdin is not None
            toolset.write_stdin.needs_approval = write_stdin_needs_approval

        capability = Shell(configure_tools=configure_tools).clone()
        capability.bind(_PtyShellSession(Manifest(root="/workspace")))

        tools = capability.tools()
        exec_command_tool = cast(ExecCommandTool, tools[0])
        write_stdin_tool = cast(WriteStdinTool, tools[1])

        assert cast(object, exec_command_tool.needs_approval) is exec_command_needs_approval
        assert cast(object, write_stdin_tool.needs_approval) is write_stdin_needs_approval

    def test_configure_tools_can_observe_missing_write_stdin_on_non_pty_session(self) -> None:
        saw_missing_write_stdin = False

        def configure_tools(toolset: ShellToolSet) -> None:
            nonlocal saw_missing_write_stdin
            saw_missing_write_stdin = toolset.write_stdin is None

        capability = Shell(configure_tools=configure_tools)
        capability.bind(_ShellSession(Manifest(root="/workspace")))

        tools = capability.tools()

        assert saw_missing_write_stdin is True
        assert len(tools) == 1
        assert isinstance(tools[0], ExecCommandTool)

    def test_configure_tools_can_replace_exec_command_tool(self) -> None:
        replacement_exec_command: ExecCommandTool | None = None

        def configure_tools(toolset: ShellToolSet) -> None:
            nonlocal replacement_exec_command
            replacement_exec_command = ExecCommandTool(
                session=toolset.exec_command.session,
                needs_approval=True,
            )
            toolset.exec_command = replacement_exec_command

        capability = Shell(configure_tools=configure_tools)
        capability.bind(_ShellSession(Manifest(root="/workspace")))

        tools = capability.tools()
        exec_command_tool = cast(ExecCommandTool, tools[0])

        assert replacement_exec_command is not None
        assert exec_command_tool is replacement_exec_command
        assert exec_command_tool.needs_approval is True

    @pytest.mark.asyncio
    async def test_instructions_match_sandbox_shell_guidance(self) -> None:
        capability = Shell()

        instructions = await capability.instructions(Manifest(root="/workspace"))

        assert (
            instructions == "When using the shell:\n"
            "- Use `exec_command` for shell execution.\n"
            "- If available, use `write_stdin` to interact with or poll running sessions.\n"
            "- To interrupt a long-running process via `write_stdin`, start it with "
            "`tty=true` and send Ctrl-C (`\\u0003`).\n"
            "- Prefer `rg` and `rg --files` for text/file discovery when available.\n"
            "- Avoid using Python scripts just to print large file chunks."
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_runs_commands_with_source_output_format(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _ShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])

        uuids = iter([uuid.UUID("12345678123456781234567812345678")])
        times = iter([100.0, 100.25])
        monkeypatch.setattr(
            "agents.sandbox.capabilities.tools.shell_tool.uuid.uuid4",
            lambda: next(uuids),
        )
        monkeypatch.setattr(
            "agents.sandbox.capabilities.tools.shell_tool.time.perf_counter",
            lambda: next(times),
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd", yield_time_ms=1500).model_dump_json(),
        )

        assert session.exec_calls == [("pwd", 1.5, True)]
        assert (
            output == "Chunk ID: 123456\n"
            "Wall time: 0.2500 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stdout: pwd\n"
            "stderr: pwd"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_runs_as_bound_user(self) -> None:
        capability = Shell()
        session = _ShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        capability.bind_run_as(User(name="sandbox-user"))
        tool = cast(FunctionTool, capability.tools()[0])

        await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd").model_dump_json(),
        )

        assert session.exec_users == ["sandbox-user"]

    @pytest.mark.asyncio
    async def test_exec_command_tool_includes_original_token_count_when_truncating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _ShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])

        uuids = iter([uuid.UUID("12345678123456781234567812345678")])
        times = iter([200.0, 200.5])
        monkeypatch.setattr(
            "agents.sandbox.capabilities.tools.shell_tool.uuid.uuid4",
            lambda: next(uuids),
        )
        monkeypatch.setattr(
            "agents.sandbox.capabilities.tools.shell_tool.time.perf_counter",
            lambda: next(times),
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd", yield_time_ms=1500, max_output_tokens=2).model_dump_json(),
        )

        assert (
            output == "Chunk ID: 123456\n"
            "Wall time: 0.5000 seconds\n"
            "Process exited with code 7\n"
            "Original token count: 6\n"
            "Output:\n"
            "Total output lines: 2\n\n"
            "stdo…4 tokens truncated… pwd"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_wraps_workdir_and_uses_custom_shell(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _ShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="87654321876543218765432187654321",
            start=300.0,
            end=300.125,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(
                cmd="pwd",
                workdir="src/project",
                shell="/bin/bash",
                login=False,
            ).model_dump_json(),
        )

        assert session.exec_calls == [
            ("cd /workspace/src/project && pwd", 10.0, ["/bin/bash", "-c"])
        ]
        assert (
            output == "Chunk ID: 876543\n"
            "Wall time: 0.1250 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stdout: cd /workspace/src/project && pwd\n"
            "stderr: cd /workspace/src/project && pwd"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_uses_pty_when_supported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _PtyShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="abcdef12abcdef12abcdef12abcdef12",
            start=400.0,
            end=400.05,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd", yield_time_ms=0, tty=True).model_dump_json(),
        )

        assert session.last_exec_yield_time_s == 0.0
        assert (
            output == "Chunk ID: abcdef\n"
            "Wall time: 0.0500 seconds\n"
            "Process running with session ID 1337\n"
            "Output:\n"
            ""
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_starts_pty_as_bound_user(self) -> None:
        capability = Shell()
        session = _PtyShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        capability.bind_run_as(User(name="sandbox-user"))
        tool = cast(FunctionTool, capability.tools()[0])

        await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd", yield_time_ms=0, tty=True).model_dump_json(),
        )

        assert session.last_exec_user == "sandbox-user"

    @pytest.mark.asyncio
    async def test_exec_command_tool_formats_timeout_without_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _TimeoutShellSession(Manifest(root="/workspace"))
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="fedcba98fedcba98fedcba98fedcba98",
            start=500.0,
            end=500.005,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="sleep 30", yield_time_ms=5).model_dump_json(),
        )

        assert (
            output == "Chunk ID: fedcba\n"
            "Wall time: 0.0050 seconds\n"
            "Output:\n"
            "Command timed out after 0.005 seconds."
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_falls_back_to_one_shot_exec_after_startup_transport_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = ExecCommandTool(
            session=_PtyTransportFailingShellSession(
                Manifest(root="/workspace"),
                stdout=b"fallback ok",
                transport_context={"stage": "open_pipe", "retry_safe": True},
            )
        )
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="44444444444444444444444444444444",
            start=510.0,
            end=510.1,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd").model_dump_json(),
        )

        assert "PTY transport failed before the interactive session opened" in output
        assert "Process exited with code 0" in output
        assert "Process running with session ID" not in output
        assert "fallback ok" in output

    @pytest.mark.asyncio
    async def test_exec_command_tool_does_not_fall_back_for_tty_sessions(self) -> None:
        tool = ExecCommandTool(
            session=_PtyTransportFailingShellSession(
                Manifest(root="/workspace"),
                transport_context={"stage": "open_pipe", "retry_safe": True, "tty": True},
            )
        )

        with pytest.raises(ExecTransportError):
            await tool.on_invoke_tool(
                cast(ToolContext[object], None),
                ExecCommandArgs(cmd="pwd", tty=True).model_dump_json(),
            )

    @pytest.mark.asyncio
    async def test_exec_command_tool_does_not_fall_back_for_non_retry_safe_transport_errors(
        self,
    ) -> None:
        tool = ExecCommandTool(
            session=_PtyTransportFailingShellSession(
                Manifest(root="/workspace"),
                transport_context={"stage": "open_pipe"},
            )
        )

        with pytest.raises(ExecTransportError):
            await tool.on_invoke_tool(
                cast(ToolContext[object], None),
                ExecCommandArgs(cmd="pwd").model_dump_json(),
            )

    @pytest.mark.asyncio
    async def test_exec_command_tool_uses_stdout_only_when_stderr_is_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = ExecCommandTool(
            session=_OutputShellSession(
                Manifest(root="/workspace"),
                stdout=b"stdout only\n",
                stderr=b"",
            )
        )
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="11111111111111111111111111111111",
            start=600.0,
            end=600.1,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd").model_dump_json(),
        )

        assert (
            output == "Chunk ID: 111111\n"
            "Wall time: 0.1000 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stdout only\n"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_uses_stderr_only_when_stdout_is_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = ExecCommandTool(
            session=_OutputShellSession(
                Manifest(root="/workspace"),
                stdout=b"",
                stderr=b"stderr only\n",
            )
        )
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="22222222222222222222222222222222",
            start=700.0,
            end=700.1,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd").model_dump_json(),
        )

        assert (
            output == "Chunk ID: 222222\n"
            "Wall time: 0.1000 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stderr only\n"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_does_not_insert_extra_newline_when_stdout_already_has_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = ExecCommandTool(
            session=_OutputShellSession(
                Manifest(root="/workspace"),
                stdout=b"stdout line\n",
                stderr=b"stderr line\n",
            )
        )
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="33333333333333333333333333333333",
            start=800.0,
            end=800.1,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd").model_dump_json(),
        )

        assert (
            output == "Chunk ID: 333333\n"
            "Wall time: 0.1000 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stdout line\n"
            "stderr line\n"
        )

    @pytest.mark.asyncio
    async def test_write_stdin_tool_writes_and_finishes_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _PtyShellSession(Manifest(root="/workspace"))
        session._live_sessions.add(1337)
        tool = WriteStdinTool(session=session)
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="55555555555555555555555555555555",
            start=900.0,
            end=900.2,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            WriteStdinArgs(session_id=1337, chars="hello").model_dump_json(),
        )

        assert (
            output == "Chunk ID: 555555\n"
            "Wall time: 0.2000 seconds\n"
            "Process exited with code 0\n"
            "Output:\n"
            "hello"
        )

    @pytest.mark.asyncio
    async def test_write_stdin_tool_rejects_non_pty_sessions(self) -> None:
        tool = WriteStdinTool(session=_ShellSession(Manifest(root="/workspace")))

        with pytest.raises(
            RuntimeError, match="write_stdin is not available for non-PTY sandboxes"
        ):
            await tool.on_invoke_tool(
                cast(ToolContext[object], None),
                WriteStdinArgs(session_id=1337).model_dump_json(),
            )

    @pytest.mark.asyncio
    async def test_write_stdin_tool_formats_unknown_session_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = WriteStdinTool(session=_PtyShellSession(Manifest(root="/workspace")))
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="66666666666666666666666666666666",
            start=910.0,
            end=910.1,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            WriteStdinArgs(session_id=9999).model_dump_json(),
        )

        assert (
            output == "Chunk ID: 666666\n"
            "Wall time: 0.1000 seconds\n"
            "Process exited with code 1\n"
            "Output:\n"
            "write_stdin failed: PTY session not found: 9999"
        )

    @pytest.mark.asyncio
    async def test_write_stdin_tool_formats_missing_stdin_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _PtyNoStdinShellSession(Manifest(root="/workspace"))
        session._live_sessions.add(1337)
        tool = WriteStdinTool(session=session)
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="77777777777777777777777777777777",
            start=920.0,
            end=920.05,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            WriteStdinArgs(session_id=1337).model_dump_json(),
        )

        assert (
            output == "Chunk ID: 777777\n"
            "Wall time: 0.0500 seconds\n"
            "Process exited with code 1\n"
            "Output:\n"
            "stdin is not available for this process. Start the command with `tty=true` in "
            "`exec_command` before using `write_stdin`."
        )
