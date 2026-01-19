from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

from .thread_options import ApprovalMode, ModelReasoningEffort, SandboxMode, WebSearchMode

_INTERNAL_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"
_TYPESCRIPT_SDK_ORIGINATOR = "codex_sdk_ts"


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
    ) -> None:
        self._executable_path = executable_path or find_codex_path()
        self._env_override = env

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
            env=env,
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

        stderr_task = asyncio.create_task(_drain_stderr())

        if process.stdin is None:
            process.kill()
            raise RuntimeError("Codex subprocess has no stdin")

        process.stdin.write(args.input.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        if process.stdout is None:
            process.kill()
            raise RuntimeError("Codex subprocess has no stdout")
        stdout = process.stdout

        cancel_task: asyncio.Task[None] | None = None
        if args.signal is not None:
            # Mirror AbortSignal semantics by terminating the subprocess.
            cancel_task = asyncio.create_task(_watch_signal(args.signal, process))

        async def _read_stdout_line() -> bytes:
            if args.idle_timeout_seconds is None:
                return await stdout.readline()

            read_task: asyncio.Task[bytes] = asyncio.create_task(stdout.readline())
            done, _ = await asyncio.wait(
                {read_task}, timeout=args.idle_timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if read_task in done:
                return read_task.result()

            if args.signal is not None:
                args.signal.set()
            if process.returncode is None:
                process.terminate()

            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(read_task, timeout=1)

            raise RuntimeError(f"Codex stream idle for {args.idle_timeout_seconds} seconds.")

        try:
            while True:
                line = await _read_stdout_line()
                if not line:
                    break
                yield line.decode("utf-8").rstrip("\n")

            await process.wait()
            if cancel_task is not None:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

            if process.returncode not in (0, None):
                await stderr_task
                stderr_text = b"".join(stderr_chunks).decode("utf-8")
                raise RuntimeError(
                    f"Codex exec exited with code {process.returncode}: {stderr_text}"
                )
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            await stderr_task
            if process.returncode is None:
                process.kill()

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


async def _watch_signal(signal: asyncio.Event, process: asyncio.subprocess.Process) -> None:
    await signal.wait()
    if process.returncode is None:
        process.terminate()


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
