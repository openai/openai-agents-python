from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

from agents.exceptions import UserError

from .thread_options import ApprovalMode, ModelReasoningEffort, SandboxMode, WebSearchMode

_INTERNAL_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"
_TYPESCRIPT_SDK_ORIGINATOR = "codex_sdk_ts"
_SUBPROCESS_STREAM_LIMIT_ENV_VAR = "OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES"
_DEFAULT_SUBPROCESS_STREAM_LIMIT_BYTES = 8 * 1024 * 1024
_MIN_SUBPROCESS_STREAM_LIMIT_BYTES = 64 * 1024
_MAX_SUBPROCESS_STREAM_LIMIT_BYTES = 64 * 1024 * 1024
_SUBPROCESS_DRAIN_CHUNK_BYTES = 64 * 1024
_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class CodexExecArgs:
    input: str
    base_url: str | None = None
    api_key: str | None = None
    thread_id: str | None = None
    images: list[str] | None = None
    model: str | None = None
    sandbox_mode: SandboxMode | None = None
    working_directory: str | None = None
    additional_directories: list[str] | None = None
    skip_git_repo_check: bool | None = None
    output_schema_file: str | None = None
    model_reasoning_effort: ModelReasoningEffort | None = None
    signal: asyncio.Event | None = None
    idle_timeout_seconds: float | None = None
    network_access_enabled: bool | None = None
    web_search_mode: WebSearchMode | None = None
    web_search_enabled: bool | None = None
    approval_policy: ApprovalMode | None = None


class CodexExec:
    def __init__(
        self,
        *,
        executable_path: str | None = None,
        env: dict[str, str] | None = None,
        subprocess_stream_limit_bytes: int | None = None,
    ) -> None:
        self._executable_path = executable_path or find_codex_path()
        self._env_override = env
        self._subprocess_stream_limit_bytes = _resolve_subprocess_stream_limit_bytes(
            subprocess_stream_limit_bytes
        )

    async def run(self, args: CodexExecArgs) -> AsyncGenerator[str, None]:
        # Build the CLI args for `codex exec --experimental-json`.
        command_args: list[str] = ["exec", "--experimental-json"]

        if args.model:
            command_args.extend(["--model", args.model])

        if args.sandbox_mode:
            command_args.extend(["--sandbox", args.sandbox_mode])

        if args.working_directory:
            command_args.extend(["--cd", args.working_directory])

        if args.additional_directories:
            for directory in args.additional_directories:
                command_args.extend(["--add-dir", directory])

        if args.skip_git_repo_check:
            command_args.append("--skip-git-repo-check")

        if args.output_schema_file:
            command_args.extend(["--output-schema", args.output_schema_file])

        if args.model_reasoning_effort:
            command_args.extend(
                ["--config", f'model_reasoning_effort="{args.model_reasoning_effort}"']
            )

        if args.network_access_enabled is not None:
            command_args.extend(
                [
                    "--config",
                    f"sandbox_workspace_write.network_access={str(args.network_access_enabled).lower()}",
                ]
            )

        if args.web_search_mode:
            command_args.extend(["--config", f'web_search="{args.web_search_mode}"'])
        elif args.web_search_enabled is True:
            command_args.extend(["--config", 'web_search="live"'])
        elif args.web_search_enabled is False:
            command_args.extend(["--config", 'web_search="disabled"'])

        if args.approval_policy:
            command_args.extend(["--config", f'approval_policy="{args.approval_policy}"'])

        if args.thread_id:
            command_args.extend(["resume", args.thread_id])

        if args.images:
            for image in args.images:
                command_args.extend(["--image", image])

        # Codex CLI expects a prompt argument; "-" tells it to read from stdin.
        command_args.append("-")

        env = self._build_env(args)

        process = await asyncio.create_subprocess_exec(
            self._executable_path,
            *command_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Codex emits one JSON event per line; large tool outputs can exceed asyncio's
            # default 64 KiB readline limit.
            limit=self._subprocess_stream_limit_bytes,
            env=env,
            # Give POSIX descendants a process-group boundary that this execution owns.
            start_new_session=os.name != "nt",
        )

        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            # Preserve stderr for error reporting without blocking stdout reads.
            if process.stderr is None:
                return
            while True:
                chunk = await process.stderr.read(1024)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        async def _drain_remaining_stdout() -> None:
            if process.stdout is None:
                return
            while await process.stdout.read(_SUBPROCESS_DRAIN_CHUNK_BYTES):
                pass

        async def _drain_stdout_and_wait() -> None:
            await _drain_remaining_stdout()
            await process.wait()

        async def _cleanup_process() -> None:
            try:
                await asyncio.wait_for(
                    _drain_stdout_and_wait(),
                    timeout=_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # A descendant may outlive the direct child while holding a pipe open.
                # Close the transport so cleanup does not wait indefinitely for EOF.
                transport = getattr(process, "_transport", None)
                if transport is not None:
                    transport.close()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS,
                    )

        stderr_task = asyncio.create_task(_drain_stderr())
        cancel_task: asyncio.Task[None] | None = None
        try:
            if process.stdin is None:
                raise RuntimeError("Codex subprocess has no stdin")

            process.stdin.write(args.input.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            if process.stdout is None:
                raise RuntimeError("Codex subprocess has no stdout")
            stdout = process.stdout

            if args.signal is not None:
                # Mirror AbortSignal semantics by terminating the subprocess.
                cancel_task = asyncio.create_task(_watch_signal(args.signal, process))

            async def _read_stdout_line() -> bytes:
                if args.idle_timeout_seconds is None:
                    return await stdout.readline()

                read_task: asyncio.Task[bytes] = asyncio.create_task(stdout.readline())
                try:
                    done, _ = await asyncio.wait(
                        {read_task},
                        timeout=args.idle_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if read_task in done:
                        return read_task.result()

                    if args.signal is not None:
                        args.signal.set()

                    raise RuntimeError(
                        f"Codex stream idle for {args.idle_timeout_seconds} seconds."
                    )
                finally:
                    if not read_task.done():
                        read_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                            await asyncio.wait_for(read_task, timeout=1)

            while True:
                line = await _read_stdout_line()
                if not line:
                    break
                yield line.decode("utf-8").rstrip("\n")

            await process.wait()
            if process.returncode not in (0, None):
                await stderr_task
                stderr_text = b"".join(stderr_chunks).decode("utf-8")
                raise RuntimeError(
                    f"Codex exec exited with code {process.returncode}: {stderr_text}"
                )
        finally:
            original_error = sys.exc_info()[1]

            async def _cleanup_resources() -> None:
                cleanup_error: BaseException | None = None

                if cancel_task is not None:
                    try:
                        await _cancel_and_wait(cancel_task)
                    except BaseException as exc:
                        cleanup_error = exc

                if original_error is not None or process.returncode is None:
                    try:
                        await _terminate_process_tree(process)
                    except BaseException as exc:
                        if cleanup_error is None:
                            cleanup_error = exc

                try:
                    await _cleanup_process()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc

                try:
                    await _cancel_and_wait(stderr_task)
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc

                if cleanup_error is not None:
                    raise cleanup_error

            cleanup_task = asyncio.create_task(_cleanup_resources())
            await _await_cleanup_task(
                cleanup_task,
                preserve_exception=original_error is not None,
            )

    def _build_env(self, args: CodexExecArgs) -> dict[str, str]:
        # Respect env overrides when provided; otherwise copy from os.environ.
        env: dict[str, str] = {}
        if self._env_override is not None:
            env.update(self._env_override)
        else:
            env.update({key: value for key, value in os.environ.items() if value is not None})

        # Preserve originator metadata used by the CLI.
        if _INTERNAL_ORIGINATOR_ENV not in env:
            env[_INTERNAL_ORIGINATOR_ENV] = _TYPESCRIPT_SDK_ORIGINATOR

        if args.base_url:
            env["OPENAI_BASE_URL"] = args.base_url
        if args.api_key:
            env["CODEX_API_KEY"] = args.api_key

        return env


async def _cancel_and_wait(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _await_cleanup_task(
    cleanup_task: asyncio.Task[None],
    *,
    preserve_exception: bool,
) -> None:
    cancellation_error: asyncio.CancelledError | None = None

    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            if cleanup_task.cancelled():
                break
            cancellation_error = exc
        except BaseException:
            break

    cleanup_error: BaseException | None = None
    try:
        cleanup_task.result()
    except BaseException as exc:
        cleanup_error = exc

    if preserve_exception:
        return
    if cancellation_error is not None:
        raise cancellation_error
    if cleanup_error is not None:
        raise cleanup_error


def _terminate_windows_process_tree(pid: int) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS,
        )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    pid = getattr(process, "pid", None)
    try:
        if pid is not None:
            if os.name == "nt":
                await asyncio.to_thread(_terminate_windows_process_tree, pid)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()


async def _watch_signal(signal_event: asyncio.Event, process: asyncio.subprocess.Process) -> None:
    await signal_event.wait()
    await _terminate_process_tree(process)


def _platform_target_triple() -> str:
    # Map the running platform to the vendor layout used in Codex releases.
    system = sys.platform
    arch = platform.machine().lower()

    if system.startswith("linux"):
        if arch in {"x86_64", "amd64"}:
            return "x86_64-unknown-linux-musl"
        if arch in {"aarch64", "arm64"}:
            return "aarch64-unknown-linux-musl"
    if system == "darwin":
        if arch in {"x86_64", "amd64"}:
            return "x86_64-apple-darwin"
        if arch in {"arm64", "aarch64"}:
            return "aarch64-apple-darwin"
    if system in {"win32", "cygwin"}:
        if arch in {"x86_64", "amd64"}:
            return "x86_64-pc-windows-msvc"
        if arch in {"arm64", "aarch64"}:
            return "aarch64-pc-windows-msvc"

    raise RuntimeError(f"Unsupported platform: {system} ({arch})")


def find_codex_path() -> str:
    # Resolution order: CODEX_PATH env, PATH lookup, bundled vendor binary.
    path_override = os.environ.get("CODEX_PATH")
    if path_override:
        return path_override

    which_path = shutil.which("codex")
    if which_path:
        return which_path

    target_triple = _platform_target_triple()
    vendor_root = Path(__file__).resolve().parent.parent.parent / "vendor"
    arch_root = vendor_root / target_triple
    binary_name = "codex.exe" if sys.platform.startswith("win") else "codex"
    binary_path = arch_root / "codex" / binary_name
    return str(binary_path)


def _resolve_subprocess_stream_limit_bytes(explicit_value: int | None) -> int:
    if explicit_value is not None:
        return _validate_subprocess_stream_limit_bytes(explicit_value)

    env_value = os.environ.get(_SUBPROCESS_STREAM_LIMIT_ENV_VAR)
    if env_value is None:
        return _DEFAULT_SUBPROCESS_STREAM_LIMIT_BYTES

    try:
        parsed = int(env_value)
    except ValueError as exc:
        raise UserError(
            f"{_SUBPROCESS_STREAM_LIMIT_ENV_VAR} must be an integer number of bytes."
        ) from exc
    return _validate_subprocess_stream_limit_bytes(parsed)


def _validate_subprocess_stream_limit_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UserError("codex_subprocess_stream_limit_bytes must be an integer number of bytes.")
    if value < _MIN_SUBPROCESS_STREAM_LIMIT_BYTES or value > _MAX_SUBPROCESS_STREAM_LIMIT_BYTES:
        raise UserError(
            "codex_subprocess_stream_limit_bytes must be between "
            f"{_MIN_SUBPROCESS_STREAM_LIMIT_BYTES} and {_MAX_SUBPROCESS_STREAM_LIMIT_BYTES} bytes."
        )
    return value
