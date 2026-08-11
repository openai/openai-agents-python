from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.capabilities import Shell, ShellToolSet
from agents.sandbox.capabilities.tools import (
    ExecCommandArgs,
    ExecCommandTool,
    WriteStdinArgs,
    WriteStdinTool,
)
from agents.sandbox.capabilities.tools.shell_tool import _resolve_shell
from agents.sandbox.errors import ExecTimeoutError, ExecTransportError, PtySessionNotFoundError
from agents.sandbox.session.pty_types import PtyExecUpdate
from agents.sandbox.types import ExecResult, User
from agents.testing import scripted_sandbox_session
from agents.tool import FunctionTool
from agents.tool_context import ToolContext


def _default_exec_result(call: Any) -> ExecResult:
    rendered_command = " ".join(str(part) for part in call.args)
    return ExecResult(
        stdout=f"stdout: {rendered_command}".encode(),
        stderr=f"stderr: {rendered_command}".encode(),
        exit_code=7,
    )


def _shell_session(
    *,
    manifest: Manifest | None = None,
    result: ExecResult | None = None,
    error: Exception | None = None,
) -> Any:
    outcome: dict[str, object]
    if error is not None:
        outcome = {"error": error}
    elif result is not None:
        outcome = {"result": result}
    else:
        outcome = {"responder": _default_exec_result}
    step: dict[str, object] = {"method": "exec"}
    step.update(outcome)
    return scripted_sandbox_session(
        cast(Any, [step]),
        manifest=manifest or Manifest(root="/workspace"),
    )


def _pty_session(
    steps: list[dict[str, object]],
    *,
    manifest: Manifest | None = None,
) -> Any:
    return scripted_sandbox_session(
        cast(Any, steps),
        manifest=manifest or Manifest(root="/workspace"),
    )


def _transport_error(context: dict[str, object]) -> ExecTransportError:
    return ExecTransportError(
        command=("pwd",),
        context=context,
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
    def test_resolve_shell_uses_plain_sh_when_login_is_false(self) -> None:
        assert _resolve_shell(None, login=False) == ["sh", "-c"]

    def test_tools_requires_bound_session(self) -> None:
        capability = Shell()

        with pytest.raises(ValueError, match="Shell capability is not bound to a SandboxSession"):
            capability.tools()

    def test_tools_exposes_exec_command_function_tool_after_bind(self) -> None:
        capability = Shell()
        capability.bind(_shell_session())

        tools = capability.tools()

        assert len(tools) == 1
        assert isinstance(tools[0], ExecCommandTool)
        assert isinstance(tools[0], FunctionTool)
        assert tools[0].name == "exec_command"

    def test_tools_exposes_write_stdin_for_pty_sessions(self) -> None:
        capability = Shell()
        capability.bind(_pty_session([{"method": "pty_write_stdin", "result": None}]))

        tools = capability.tools()

        assert len(tools) == 2
        assert isinstance(tools[0], ExecCommandTool)
        assert isinstance(tools[1], WriteStdinTool)
        assert tools[0].name == "exec_command"
        assert tools[1].name == "write_stdin"

    def test_tools_keep_both_pty_session_methods_callable(self) -> None:
        capability = Shell()
        session = _pty_session([{"method": "pty_exec_start", "result": None}])
        capability.bind(session)

        tools = capability.tools()

        assert len(tools) == 2
        assert hasattr(session, "pty_exec_start")
        assert hasattr(session, "pty_write_stdin")

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
        capability.bind(_pty_session([{"method": "pty_write_stdin", "result": None}]))

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
        capability.bind(_shell_session())

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
        capability.bind(_shell_session())

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
        session = _shell_session()
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

        assert session.calls[0].args == ("pwd",)
        assert session.calls[0].kwargs["timeout"] == 1.5
        assert session.calls[0].kwargs["shell"] is True
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
        session = scripted_sandbox_session(
            [
                {
                    "method": "exec",
                    "result": ExecResult(stdout=b"", stderr=b"", exit_code=0),
                }
            ]
        )
        capability.bind(session)
        capability.bind_run_as(User(name="sandbox-user"))
        tool = cast(FunctionTool, capability.tools()[0])

        await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd").model_dump_json(),
        )

        assert session.calls[0].kwargs["user"] == User(name="sandbox-user")
        session.assert_complete()

    @pytest.mark.asyncio
    async def test_exec_command_tool_includes_original_token_count_when_truncating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _shell_session()
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
            "…6 tok"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_wraps_workdir_and_uses_custom_shell(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _shell_session()
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

        assert session.calls[0].args == ("cd /workspace/src/project && pwd",)
        assert session.calls[0].kwargs["timeout"] == 10.0
        assert session.calls[0].kwargs["shell"] == ["/bin/bash", "-c"]
        assert (
            output == "Chunk ID: 876543\n"
            "Wall time: 0.1250 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stdout: cd /workspace/src/project && pwd\n"
            "stderr: cd /workspace/src/project && pwd"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_allows_split_path_grant_workdir(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _shell_session(
            manifest=Manifest(
                root="/workspace",
                extra_path_grants=(
                    SandboxPathGrant(
                        path="/mnt/shared-data",
                        host_path="/native/shared-data",
                        read_only=True,
                    ),
                ),
            )
        )
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])
        _patch_shell_tool_clock(
            monkeypatch,
            chunk_id="11111111111111111111111111111111",
            start=310.0,
            end=310.25,
        )

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(
                cmd="pwd",
                workdir="/mnt/shared-data",
                shell="/bin/bash",
                login=False,
            ).model_dump_json(),
        )

        assert session.calls[0].args == ("cd /mnt/shared-data && pwd",)
        assert session.calls[0].kwargs["timeout"] == 10.0
        assert session.calls[0].kwargs["shell"] == ["/bin/bash", "-c"]
        assert (
            output == "Chunk ID: 111111\n"
            "Wall time: 0.2500 seconds\n"
            "Process exited with code 7\n"
            "Output:\n"
            "stdout: cd /mnt/shared-data && pwd\n"
            "stderr: cd /mnt/shared-data && pwd"
        )

    @pytest.mark.asyncio
    async def test_exec_command_tool_uses_pty_when_supported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _pty_session(
            [
                {
                    "method": "pty_exec_start",
                    "result": PtyExecUpdate(
                        process_id=1337,
                        output=b"",
                        exit_code=None,
                        original_token_count=None,
                    ),
                }
            ]
        )
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

        assert session.calls[0].kwargs["yield_time_s"] == 0.0
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
        session = _pty_session(
            [
                {
                    "method": "pty_exec_start",
                    "result": PtyExecUpdate(
                        process_id=1337,
                        output=b"",
                        exit_code=None,
                        original_token_count=None,
                    ),
                }
            ]
        )
        capability.bind(session)
        capability.bind_run_as(User(name="sandbox-user"))
        tool = cast(FunctionTool, capability.tools()[0])

        await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            ExecCommandArgs(cmd="pwd", yield_time_ms=0, tty=True).model_dump_json(),
        )

        assert session.calls[0].kwargs["user"] == User(name="sandbox-user")

    @pytest.mark.asyncio
    async def test_exec_command_tool_formats_timeout_without_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capability = Shell()
        session = _shell_session(error=ExecTimeoutError(command=("sleep 30",), timeout_s=0.005))
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
        session = _pty_session(
            [
                {
                    "method": "pty_exec_start",
                    "error": _transport_error({"stage": "open_pipe", "retry_safe": True}),
                },
                {
                    "method": "exec",
                    "result": ExecResult(stdout=b"fallback ok", stderr=b"", exit_code=0),
                },
            ]
        )
        tool = ExecCommandTool(session=session)
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
        session = _pty_session(
            [
                {
                    "method": "pty_exec_start",
                    "error": _transport_error(
                        {"stage": "open_pipe", "retry_safe": True, "tty": True}
                    ),
                }
            ]
        )
        tool = ExecCommandTool(session=session)

        with pytest.raises(ExecTransportError):
            await tool.on_invoke_tool(
                cast(ToolContext[object], None),
                ExecCommandArgs(cmd="pwd", tty=True).model_dump_json(),
            )

    @pytest.mark.asyncio
    async def test_exec_command_tool_does_not_fall_back_for_non_retry_safe_transport_errors(
        self,
    ) -> None:
        session = _pty_session(
            [
                {
                    "method": "pty_exec_start",
                    "error": _transport_error({"stage": "open_pipe"}),
                }
            ]
        )
        tool = ExecCommandTool(session=session)

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
            session=_shell_session(
                result=ExecResult(stdout=b"stdout only\n", stderr=b"", exit_code=7)
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
            session=_shell_session(
                result=ExecResult(stdout=b"", stderr=b"stderr only\n", exit_code=7)
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
            session=_shell_session(
                result=ExecResult(
                    stdout=b"stdout line\n",
                    stderr=b"stderr line\n",
                    exit_code=7,
                )
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
        session = _pty_session(
            [
                {
                    "method": "pty_write_stdin",
                    "result": PtyExecUpdate(
                        process_id=None,
                        output=b"hello",
                        exit_code=0,
                        original_token_count=None,
                    ),
                }
            ]
        )
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
        tool = WriteStdinTool(session=_shell_session())

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
        session = _pty_session(
            [
                {
                    "method": "pty_write_stdin",
                    "error": PtySessionNotFoundError(session_id=9999),
                }
            ]
        )
        tool = WriteStdinTool(session=session)
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
        session = _pty_session(
            [
                {
                    "method": "pty_write_stdin",
                    "error": RuntimeError("stdin is not available for this process"),
                }
            ]
        )
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

    @pytest.mark.asyncio
    async def test_write_stdin_tool_reraises_unexpected_runtime_error(self) -> None:
        session = _pty_session(
            [
                {
                    "method": "pty_write_stdin",
                    "error": RuntimeError("unexpected stdin failure"),
                }
            ]
        )
        tool = WriteStdinTool(session=session)

        with pytest.raises(RuntimeError, match="unexpected stdin failure"):
            await tool.on_invoke_tool(
                cast(ToolContext[object], None),
                WriteStdinArgs(session_id=1337).model_dump_json(),
            )
