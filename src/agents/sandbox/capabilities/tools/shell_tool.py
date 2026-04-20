from __future__ import annotations

import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....run_context import RunContextWrapper
from ....tool import FunctionTool
from ...errors import ExecTimeoutError, ExecTransportError, PtySessionNotFoundError
from ...session.base_sandbox_session import BaseSandboxSession
from ...types import User
from ...util.token_truncation import formatted_truncate_text_with_token_count
from ...workspace_paths import sandbox_path_str

_DEFAULT_EXEC_YIELD_TIME_MS = 10_000
_DEFAULT_WRITE_STDIN_YIELD_TIME_MS = 250
_TOOL_OUTPUT_HEADER = "Output:"


def _truncate_output(text: str, max_output_tokens: int | None) -> tuple[str, int | None]:
    return formatted_truncate_text_with_token_count(text, max_output_tokens)


def _supports_transport_fallback(exc: ExecTransportError) -> bool:
    return exc.context.get("retry_safe") is True


def _format_response(
    *,
    output: str,
    wall_time_seconds: float,
    exit_code: int | None,
    process_id: int | None = None,
    original_token_count: int | None = None,
) -> str:
    sections = [f"Chunk ID: {uuid.uuid4().hex[:6]}", f"Wall time: {wall_time_seconds:.4f} seconds"]

    if exit_code is not None:
        sections.append(f"Process exited with code {exit_code}")
    if process_id is not None:
        sections.append(f"Process running with session ID {process_id}")
    if original_token_count is not None:
        sections.append(f"Original token count: {original_token_count}")

    sections.append(_TOOL_OUTPUT_HEADER)
    sections.append(output)
    return "\n".join(sections)


def _prepend_notice(output: str, notice: str) -> str:
    return notice if output == "" else f"{notice}\n{output}"


def _normalize_output(stdout: bytes, stderr: bytes) -> str:
    decoded_stdout = stdout.decode("utf-8", errors="replace")
    decoded_stderr = stderr.decode("utf-8", errors="replace")

    if decoded_stdout and decoded_stderr:
        joiner = "" if decoded_stdout.endswith("\n") else "\n"
        return f"{decoded_stdout}{joiner}{decoded_stderr}"
    return decoded_stdout or decoded_stderr


def _resolve_workdir_command(
    *, session: BaseSandboxSession, command: str, workdir: str | None
) -> str:
    if workdir is None or workdir.strip() == "":
        return command

    resolved_workdir = session.normalize_path(Path(workdir))
    return f"cd {shlex.quote(sandbox_path_str(resolved_workdir))} && {command}"


def _resolve_shell(shell: str | None, login: bool) -> bool | list[str]:
    if shell is None:
        if login:
            return True
        return ["sh", "-c"]

    flag = "-lc" if login else "-c"
    return [shell, flag]


async def _run_one_shot_exec(
    *,
    session: BaseSandboxSession,
    command: str,
    timeout_s: float | None,
    shell: bool | list[str],
    max_output_tokens: int | None,
    user: str | User | None = None,
) -> tuple[str, int, int | None]:
    result = await session.exec(command, timeout=timeout_s, shell=shell, user=user)
    output = _normalize_output(result.stdout, result.stderr)
    output, original_token_count = _truncate_output(output, max_output_tokens)
    return output, result.exit_code, original_token_count


class ExecCommandArgs(BaseModel):
    cmd: str = Field(description="Shell command to execute.", min_length=1)
    workdir: str | None = Field(
        default=None,
        description="Optional working directory to run the command in; defaults to the turn cwd.",
    )
    shell: str | None = Field(
        default=None, description="Shell binary to launch. Defaults to the user's default shell."
    )
    login: bool = Field(
        default=True, description="Whether to run the shell with -l/-i semantics. Defaults to true."
    )
    tty: bool = Field(
        default=False,
        description=(
            "Whether to allocate a TTY for the command. Defaults to false (plain pipes); set to "
            "true to open a PTY and access TTY process."
        ),
    )
    yield_time_ms: int = Field(
        default=_DEFAULT_EXEC_YIELD_TIME_MS,
        ge=0,
        description="How long to wait (in milliseconds) for output before yielding.",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of tokens to return. Excess output will be truncated.",
    )


class WriteStdinArgs(BaseModel):
    session_id: int = Field(description="Identifier of the running unified exec session.")
    chars: str = Field(default="", description="Bytes to write to stdin (may be empty to poll).")
    yield_time_ms: int = Field(
        default=_DEFAULT_WRITE_STDIN_YIELD_TIME_MS,
        ge=0,
        description="How long to wait (in milliseconds) for output before yielding.",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of tokens to return. Excess output will be truncated.",
    )


@dataclass(init=False)
class ExecCommandTool(FunctionTool):
    tool_name: ClassVar[str] = "exec_command"
    args_model: ClassVar[type[ExecCommandArgs]] = ExecCommandArgs
    tool_description: ClassVar[str] = (
        "Runs a command in a PTY, returning output or a session ID for ongoing interaction."
    )
    session: BaseSandboxSession = field(init=False, repr=False, compare=False)
    user: str | User | None = field(default=None, init=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        user: str | User | None = None,
        needs_approval: (
            bool | Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]]
        ) = False,
    ) -> None:
        self.session = session
        self.user = user
        super().__init__(
            name=self.tool_name,
            description=self.tool_description,
            params_json_schema=self.args_model.model_json_schema(),
            on_invoke_tool=self._invoke,
            strict_json_schema=False,
            needs_approval=needs_approval,
        )

    async def _invoke(self, _: object, raw_input: str) -> str:
        return await self.run(self.args_model.model_validate_json(raw_input))

    async def run(self, args: ExecCommandArgs) -> str:
        start = time.perf_counter()
        timeout_s = args.yield_time_ms / 1000
        wrapped_command = _resolve_workdir_command(
            session=self.session, command=args.cmd, workdir=args.workdir
        )
        shell = _resolve_shell(args.shell, args.login)
        fallback_notice: str | None = None

        try:
            if self.session.supports_pty():
                try:
                    update = await self.session.pty_exec_start(
                        wrapped_command,
                        shell=shell,
                        tty=args.tty,
                        user=self.user,
                        yield_time_s=timeout_s,
                        max_output_tokens=args.max_output_tokens,
                    )
                    output = update.output.decode("utf-8", errors="replace")
                    exit_code = update.exit_code
                    process_id = update.process_id
                    original_token_count = update.original_token_count
                except ExecTransportError as exc:
                    if args.tty or not _supports_transport_fallback(exc):
                        raise
                    output, exit_code, original_token_count = await _run_one_shot_exec(
                        session=self.session,
                        command=wrapped_command,
                        timeout_s=timeout_s,
                        shell=shell,
                        max_output_tokens=args.max_output_tokens,
                        user=self.user,
                    )
                    process_id = None
                    fallback_notice = (
                        "PTY transport failed before the interactive session opened; "
                        "fell back to one-shot exec."
                    )
            else:
                output, exit_code, original_token_count = await _run_one_shot_exec(
                    session=self.session,
                    command=wrapped_command,
                    timeout_s=timeout_s,
                    shell=shell,
                    max_output_tokens=args.max_output_tokens,
                    user=self.user,
                )
                process_id = None
        except (ExecTimeoutError, TimeoutError):
            output = f"Command timed out after {timeout_s:.3f} seconds."
            exit_code = None
            process_id = None
            original_token_count = None

        if fallback_notice is not None:
            output = _prepend_notice(output, fallback_notice)

        return _format_response(
            output=output,
            wall_time_seconds=time.perf_counter() - start,
            exit_code=exit_code,
            process_id=process_id,
            original_token_count=original_token_count,
        )


@dataclass(init=False)
class WriteStdinTool(FunctionTool):
    tool_name: ClassVar[str] = "write_stdin"
    args_model: ClassVar[type[WriteStdinArgs]] = WriteStdinArgs
    tool_description: ClassVar[str] = (
        "Writes characters to an existing unified exec session and returns recent output."
    )
    session: BaseSandboxSession = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        needs_approval: (
            bool | Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]]
        ) = False,
    ) -> None:
        self.session = session
        super().__init__(
            name=self.tool_name,
            description=self.tool_description,
            params_json_schema=self.args_model.model_json_schema(),
            on_invoke_tool=self._invoke,
            strict_json_schema=False,
            needs_approval=needs_approval,
        )

    async def _invoke(self, _: object, raw_input: str) -> str:
        return await self.run(self.args_model.model_validate_json(raw_input))

    async def run(self, args: WriteStdinArgs) -> str:
        if not self.session.supports_pty():
            raise RuntimeError("write_stdin is not available for non-PTY sandboxes")

        start = time.perf_counter()
        yield_time_s = args.yield_time_ms / 1000
        try:
            update = await self.session.pty_write_stdin(
                session_id=args.session_id,
                chars=args.chars,
                yield_time_s=yield_time_s,
                max_output_tokens=args.max_output_tokens,
            )
        except PtySessionNotFoundError as exc:
            return _format_response(
                output=f"write_stdin failed: {exc}",
                wall_time_seconds=time.perf_counter() - start,
                exit_code=1,
                process_id=None,
                original_token_count=None,
            )
        except RuntimeError as exc:
            if str(exc) != "stdin is not available for this process":
                raise
            return _format_response(
                output=(
                    "stdin is not available for this process. "
                    "Start the command with `tty=true` in `exec_command` before using "
                    "`write_stdin`."
                ),
                wall_time_seconds=time.perf_counter() - start,
                exit_code=1,
                process_id=None,
                original_token_count=None,
            )

        return _format_response(
            output=update.output.decode("utf-8", errors="replace"),
            wall_time_seconds=time.perf_counter() - start,
            exit_code=update.exit_code,
            process_id=update.process_id,
            original_token_count=update.original_token_count,
        )
