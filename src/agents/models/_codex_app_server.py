from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..exceptions import UserError


def resolve_codex_executable(configured: Path | None) -> str:
    """Select the same executable for every App Server operation, including native tools."""
    candidate = str(configured) if configured is not None else shutil.which("codex")
    if candidate is None:
        raise UserError("Install Codex on PATH or set CodexProvider(codex_executable=...).")
    path = Path(candidate).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise UserError(f"Codex executable is missing or cannot be executed: {path}")
    return str(path)


_INITIALIZE_RETRY_DELAYS = (0.2, 0.8, 2.0)


class _CodexProcessExitedError(RuntimeError):
    """The child closed its protocol stream before completing a request."""


class CodexAppServer:
    """One stock Codex JSONL connection, owned by one model attempt."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any] | Exception] = asyncio.Queue()
        self._failure: Exception | None = None
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def open(
        cls,
        executable: str,
        cwd: str,
        config_overrides: Sequence[str],
        env: Mapping[str, str] | None,
    ) -> CodexAppServer:
        args = [
            executable,
            *([] if Path(executable).stem == "codex-app-server" else ["app-server"]),
            "--config",
            "features.apps=false",
            "--config",
            'web_search="disabled"',
        ]
        for override in config_overrides:
            args.extend(("--config", override))
        failure = False
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
            )
        except OSError:
            failure = True
        if failure:
            raise UserError("Unable to start Codex App Server.")
        return cls(process)

    @classmethod
    async def open_initialized(
        cls,
        executable: str,
        cwd: str,
        config_overrides: Sequence[str],
        env: Mapping[str, str] | None,
    ) -> CodexAppServer:
        for attempt in range(len(_INITIALIZE_RETRY_DELAYS) + 1):
            server = await cls.open(executable, cwd, config_overrides, env)
            try:
                await server.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "openai_agents_sdk", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
            except _CodexProcessExitedError:
                await server.close()
                if attempt == len(_INITIALIZE_RETRY_DELAYS):
                    raise
                await asyncio.sleep(_INITIALIZE_RETRY_DELAYS[attempt])
            except BaseException:
                await server.close()
                raise
            else:
                try:
                    await server.notify("initialized")
                except BaseException:
                    await server.close()
                    raise
                return server
        raise AssertionError("Initialization attempts exhausted.")

    def _fail(self, error: Exception) -> None:
        if self._failure is not None:
            return
        self._failure = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._events.put_nowait(error)

    async def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                invalid = False
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        invalid = True
                    elif isinstance(message.get("method"), str):
                        self._events.put_nowait(message)
                    elif isinstance(message.get("id"), int) and (
                        "result" in message or "error" in message
                    ):
                        future = self._pending.pop(message["id"], None)
                        if future is not None and not future.done():
                            if "error" in message:
                                future.set_exception(
                                    RuntimeError("Codex App Server request failed.")
                                )
                            else:
                                future.set_result(message["result"])
                    else:
                        invalid = True
                except (ValueError, TypeError):
                    invalid = True
                if invalid:
                    self._fail(RuntimeError("Invalid Codex App Server protocol message."))
                    return
            self._fail(
                _CodexProcessExitedError("Codex App Server exited before completing the response.")
            )
        except (OSError, ValueError):
            self._fail(RuntimeError("Unable to read from Codex App Server."))

    async def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        while await self._process.stderr.read(65536):
            pass

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"method": method, **({"params": params} if params is not None else {})})

    async def _write(self, message: dict[str, Any]) -> None:
        if self._failure is not None:
            raise self._failure
        assert self._process.stdin is not None
        failed = False
        try:
            self._process.stdin.write((json.dumps(message) + "\n").encode())
            await self._process.stdin.drain()
        except (OSError, ConnectionError):
            failed = True
        if failed:
            error = RuntimeError("Unable to write to Codex App Server.")
            self._fail(error)
            raise error

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._events.get()
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self) -> None:
        self._fail(RuntimeError("Codex App Server closed."))
        if self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), 1)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        for task in (self._reader, self._stderr):
            task.cancel()
        await asyncio.gather(self._reader, self._stderr, return_exceptions=True)
