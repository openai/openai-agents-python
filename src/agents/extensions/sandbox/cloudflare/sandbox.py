"""
Cloudflare sandbox (https://developers.cloudflare.com/sandbox/) implementation.

This module provides a Cloudflare Worker-backed sandbox client/session implementation.
The sandbox communicates with a Cloudflare Worker service over HTTP and WebSocket.

Note: The `aiohttp` dependency is intended to be optional (installed via an extra),
so package-level exports should guard imports of this module. Within this module,
we import aiohttp normally so IDEs can resolve and navigate types.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shlex
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import aiohttp

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    MountConfigError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.mount_lifecycle import with_ephemeral_mounts_removed
from ....sandbox.session.pty_types import (
    PTY_PROCESSES_MAX,
    PTY_PROCESSES_WARNING,
    PtyExecUpdate,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
    truncate_text_by_tokens,
)
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

_DEFAULT_EXEC_TIMEOUT_S = 30.0
_DEFAULT_REQUEST_TIMEOUT_S = 120.0

logger = logging.getLogger(__name__)


def _is_transient_workspace_error(exc: BaseException) -> bool:
    """Return True if *exc* is a workspace archive error caused by a transient HTTP status."""
    if not isinstance(exc, WorkspaceArchiveReadError | WorkspaceArchiveWriteError):
        return False
    status = exc.context.get("http_status")
    return isinstance(status, int) and status in TRANSIENT_HTTP_STATUS_CODES


@dataclass
class _ServerSentEvent:
    event: str = "message"
    data: str = ""
    id: str = ""
    retry: int | None = None


class _SSELineDecoder:
    _buf: bytes

    def __init__(self) -> None:
        self._buf = b""

    def decode(self, text: str) -> list[str]:
        raw = self._buf + text.encode("utf-8")
        self._buf = b""

        lines: list[str] = []
        i = 0
        length = len(raw)
        while i < length:
            cr = raw.find(b"\r", i)
            lf = raw.find(b"\n", i)

            if cr == -1 and lf == -1:
                self._buf = raw[i:]
                break

            if cr != -1 and (lf == -1 or cr < lf):
                line = raw[i:cr]
                if cr + 1 < length and raw[cr + 1 : cr + 2] == b"\n":
                    i = cr + 2
                elif cr + 1 == length:
                    self._buf = b"\r"
                    lines.append(line.decode("utf-8"))
                    break
                else:
                    i = cr + 1
                lines.append(line.decode("utf-8"))
            else:
                line = raw[i:lf]
                i = lf + 1
                lines.append(line.decode("utf-8"))

        return lines

    def flush(self) -> list[str]:
        buf = self._buf
        self._buf = b""
        if buf == b"\r":
            return [""]
        if buf:
            return [buf.decode("utf-8")]
        return []


class _SSEDecoder:
    _event: str | None
    _data: list[str]
    _last_event_id: str | None
    _retry: int | None

    def __init__(self) -> None:
        self._event = None
        self._data = []
        self._last_event_id = None
        self._retry = None

    def decode(self, line: str) -> _ServerSentEvent | None:
        if not line:
            if (
                not self._event
                and not self._data
                and self._last_event_id is None
                and self._retry is None
            ):
                return None

            sse = _ServerSentEvent(
                event=self._event or "message",
                data="\n".join(self._data),
                id=self._last_event_id or "",
                retry=self._retry,
            )

            self._event = None
            self._data = []
            self._retry = None
            return sse

        if line.startswith(":"):
            return None

        fieldname, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]

        if fieldname == "event":
            self._event = value
        elif fieldname == "data":
            self._data.append(value)
        elif fieldname == "id":
            if "\0" not in value:
                self._last_event_id = value
        elif fieldname == "retry":
            try:
                self._retry = int(value)
            except (TypeError, ValueError):
                pass

        return None


class CloudflareSandboxClientOptions(BaseSandboxClientOptions):
    """Options for ``CloudflareSandboxClient``."""

    type: Literal["cloudflare"] = "cloudflare"
    worker_url: str
    api_key: str | None = None
    exposed_ports: tuple[int, ...] = ()

    def __init__(
        self,
        worker_url: str,
        api_key: str | None = None,
        exposed_ports: tuple[int, ...] = (),
        *,
        type: Literal["cloudflare"] = "cloudflare",
    ) -> None:
        super().__init__(
            type=type,
            worker_url=worker_url,
            api_key=api_key,
            exposed_ports=exposed_ports,
        )


class CloudflareSandboxSessionState(SandboxSessionState):
    type: Literal["cloudflare"] = "cloudflare"
    worker_url: str
    sandbox_id: str


@dataclass
class _CloudflarePtyProcessEntry:
    """Per-process state for a Cloudflare WebSocket PTY session."""

    ws: aiohttp.ClientWebSocketResponse
    tty: bool
    last_used: float = field(default_factory=time.monotonic)
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    output_closed: asyncio.Event = field(default_factory=asyncio.Event)
    pump_task: asyncio.Task[None] | None = None
    exit_code: int | None = None


class CloudflareSandboxSession(BaseSandboxSession):
    """``BaseSandboxSession`` backed by a Cloudflare Worker over HTTP."""

    state: CloudflareSandboxSessionState
    _api_key: str | None
    _http: aiohttp.ClientSession | None
    _exec_timeout_s: float | None
    _request_timeout_s: float | None
    _pty_lock: asyncio.Lock
    _pty_processes: dict[int, _CloudflarePtyProcessEntry]
    _reserved_pty_process_ids: set[int]
    # Tracks whether the worker was running when resume began so snapshot restore can
    # detach any active ephemeral mounts before hydrating the workspace.
    _restore_workspace_was_running: bool

    def __init__(
        self,
        *,
        state: CloudflareSandboxSessionState,
        http: aiohttp.ClientSession | None = None,
        api_key: str | None = None,
        exec_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
    ) -> None:
        self.state = state
        self._api_key = api_key
        self._http = http
        self._exec_timeout_s = exec_timeout_s
        self._request_timeout_s = request_timeout_s
        self._pty_lock = asyncio.Lock()
        self._pty_processes = {}
        self._reserved_pty_process_ids = set()
        self._restore_workspace_was_running = False

    @classmethod
    def from_state(
        cls,
        state: CloudflareSandboxSessionState,
        *,
        http: aiohttp.ClientSession | None = None,
        exec_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
    ) -> CloudflareSandboxSession:
        return cls(
            state=state,
            http=http,
            exec_timeout_s=exec_timeout_s,
            request_timeout_s=request_timeout_s,
        )

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            headers: dict[str, str] = {}
            if api_key := self._api_key or os.environ.get("CLOUDFLARE_SANDBOX_API_KEY"):
                headers["Authorization"] = f"Bearer {api_key}"
            self._http = aiohttp.ClientSession(headers=headers)
        return self._http

    def _url(self, path: str) -> str:
        base = self.state.worker_url.rstrip("/")
        return f"{base}/v1/sandbox/{self.state.sandbox_id}/{path.lstrip('/')}"

    def _ws_pty_url(self, *, cols: int = 80, rows: int = 24) -> str:
        base = self.state.worker_url.rstrip("/")
        if base.startswith("https://"):
            ws_base = f"wss://{base.removeprefix('https://')}"
        elif base.startswith("http://"):
            ws_base = f"ws://{base.removeprefix('http://')}"
        else:
            ws_base = base
        return f"{ws_base}/v1/sandbox/{self.state.sandbox_id}/pty?cols={cols}&rows={rows}"

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.sandbox_id

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        """Cloudflare sandboxes do not yet support exposed port resolution."""
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context={
                "backend": "cloudflare",
                "detail": (
                    "The Cloudflare sandbox worker does not currently expose "
                    "a port-resolution endpoint. Exposed port support requires "
                    "a compatible worker deployment."
                ),
            },
        )

    async def mount_bucket(
        self,
        *,
        bucket: str,
        mount_path: Path | str,
        options: dict[str, object],
    ) -> None:
        workspace_path = await self._validate_path_access(
            coerce_posix_path(mount_path).as_posix(), for_write=True
        )
        http = self._session()
        url = self._url("mount")
        payload = {
            "bucket": bucket,
            "mountPath": sandbox_path_str(workspace_path),
            "options": options,
        }

        try:
            async with http.post(
                url,
                json=payload,
                timeout=self._request_timeout(),
            ) as resp:
                if resp.status != 200:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise MountConfigError(
                        message="cloudflare bucket mount failed",
                        context={
                            "bucket": bucket,
                            "mount_path": sandbox_path_str(workspace_path),
                            "http_status": resp.status,
                            "reason": body.get("error", f"HTTP {resp.status}"),
                        },
                    )
        except MountConfigError:
            raise
        except aiohttp.ClientError as e:
            raise MountConfigError(
                message="cloudflare bucket mount failed",
                context={
                    "bucket": bucket,
                    "mount_path": sandbox_path_str(workspace_path),
                    "cause_type": type(e).__name__,
                    "reason": str(e),
                },
            ) from e

    async def unmount_bucket(self, mount_path: Path | str) -> None:
        workspace_path = await self._validate_path_access(
            coerce_posix_path(mount_path).as_posix(), for_write=True
        )
        http = self._session()
        url = self._url("unmount")
        payload = {"mountPath": sandbox_path_str(workspace_path)}

        try:
            async with http.post(
                url,
                json=payload,
                timeout=self._request_timeout(),
            ) as resp:
                if resp.status != 200:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise MountConfigError(
                        message="cloudflare bucket unmount failed",
                        context={
                            "mount_path": sandbox_path_str(workspace_path),
                            "http_status": resp.status,
                            "reason": body.get("error", f"HTTP {resp.status}"),
                        },
                    )
        except MountConfigError:
            raise
        except aiohttp.ClientError as e:
            raise MountConfigError(
                message="cloudflare bucket unmount failed",
                context={
                    "mount_path": sandbox_path_str(workspace_path),
                    "cause_type": type(e).__name__,
                    "reason": str(e),
                },
            ) from e

    async def _close_http(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    def _request_timeout(self) -> aiohttp.ClientTimeout:
        total = (
            self._request_timeout_s
            if self._request_timeout_s is not None
            else _DEFAULT_REQUEST_TIMEOUT_S
        )
        return aiohttp.ClientTimeout(total=total)

    def _decode_streamed_payload(self, body: bytes) -> bytes:
        if not body.startswith(b"data: {"):
            return body

        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body

        line_decoder = _SSELineDecoder()
        sse_decoder = _SSEDecoder()
        is_binary = False
        chunks: list[bytes] = []
        saw_metadata = False
        saw_chunk = False
        saw_complete = False

        def _handle_event_payload(data: str) -> None:
            nonlocal is_binary, saw_complete, saw_chunk, saw_metadata
            message = json.loads(data)
            msg_type = message.get("type")
            if msg_type == "metadata":
                is_binary = bool(message.get("isBinary", False))
                saw_metadata = True
                return
            if msg_type == "chunk":
                if not saw_metadata:
                    raise ValueError("chunk event received before metadata")
                chunk = message.get("data", "")
                if is_binary:
                    chunks.append(base64.b64decode(chunk))
                else:
                    chunks.append(str(chunk).encode("utf-8"))
                saw_chunk = True
                return
            if msg_type == "complete":
                if not saw_metadata:
                    raise ValueError("complete event received before metadata")
                saw_complete = True
                return

        try:
            for line in line_decoder.decode(text):
                event = sse_decoder.decode(line)
                if event is not None and event.event == "message" and event.data:
                    _handle_event_payload(event.data)

            for line in line_decoder.flush():
                event = sse_decoder.decode(line)
                if event is not None and event.event == "message" and event.data:
                    _handle_event_payload(event.data)
        except (ValueError, json.JSONDecodeError):
            return body

        if not saw_metadata or (not saw_chunk and not saw_complete):
            return body
        if not saw_complete:
            raise ValueError("SSE payload ended without complete event")
        return b"".join(chunks)

    async def _prepare_backend_workspace(self) -> None:
        try:
            root = self._workspace_root_path()
            await self._exec_internal("mkdir", "-p", "--", root.as_posix())
        except Exception as e:
            raise WorkspaceStartError(path=self._workspace_root_path(), cause=e) from e

    async def _can_reuse_restorable_snapshot_workspace(self) -> bool:
        if not self._workspace_state_preserved_on_start():
            self._restore_workspace_was_running = False
            return False

        is_running = await self.running()
        self._restore_workspace_was_running = is_running
        if not self._can_reuse_preserved_workspace_on_resume():
            return False
        return await self._can_skip_snapshot_restore_on_resume(is_running=is_running)

    async def _restore_snapshot_into_workspace_on_resume(self) -> None:
        root = self._workspace_root_path()
        detached_mounts: list[tuple[Any, Path]] = []
        if self._restore_workspace_was_running:
            for mount_entry, mount_path in self.state.manifest.ephemeral_mount_targets():
                try:
                    await mount_entry.mount_strategy.teardown_for_snapshot(
                        mount_entry, self, mount_path
                    )
                except Exception as e:
                    raise WorkspaceStartError(path=root, cause=e) from e
                detached_mounts.append((mount_entry, mount_path))

        workspace_archive: io.IOBase | None = None
        try:
            await self._clear_workspace_root_on_resume()
            workspace_archive = await self.state.snapshot.restore(dependencies=self.dependencies)
            await self._hydrate_workspace_via_http(workspace_archive)
        except Exception:
            for mount_entry, mount_path in reversed(detached_mounts):
                try:
                    await mount_entry.mount_strategy.restore_after_snapshot(
                        mount_entry, self, mount_path
                    )
                except Exception:
                    pass
            raise
        finally:
            if workspace_archive is not None:
                try:
                    workspace_archive.close()
                except Exception:
                    pass

    async def _after_stop(self) -> None:
        await self._close_http()

    async def _shutdown_backend(self) -> None:
        try:
            http = self._session()
            url = self.state.worker_url.rstrip("/") + f"/v1/sandbox/{self.state.sandbox_id}"
            async with http.delete(url):
                pass
        except Exception:
            logger.debug("Failed to delete Cloudflare sandbox on shutdown", exc_info=True)

    async def _after_shutdown(self) -> None:
        await self._close_http()

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        argv = [str(c) for c in command]
        envs = await self.state.manifest.environment.resolve()
        if envs:
            argv = ["env", *[f"{key}={value}" for key, value in sorted(envs.items())], *argv]
        effective_timeout = (
            timeout
            if timeout is not None
            else (
                self._exec_timeout_s
                if self._exec_timeout_s is not None
                else _DEFAULT_EXEC_TIMEOUT_S
            )
        )
        payload: dict[str, Any] = {"argv": argv}
        if effective_timeout is not None:
            payload["timeout_ms"] = int(effective_timeout * 1000)

        http = self._session()
        url = self._url("exec")

        try:
            request_timeout = aiohttp.ClientTimeout(
                total=effective_timeout + 5.0 if effective_timeout is not None else None
            )
            async with http.post(url, json=payload, timeout=request_timeout) as resp:
                if resp.status != 200:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    msg = body.get("error", f"HTTP {resp.status}")
                    raise ExecTransportError(command=tuple(argv), cause=Exception(msg))

                stdout_parts: list[bytes] = []
                stderr_parts: list[bytes] = []
                line_decoder = _SSELineDecoder()
                sse_decoder = _SSEDecoder()

                async for chunk in resp.content.iter_any():
                    text = chunk.decode("utf-8")
                    for line in line_decoder.decode(text):
                        event = sse_decoder.decode(line)
                        if event is None:
                            continue
                        if event.event == "stdout":
                            stdout_parts.append(base64.b64decode(event.data))
                        elif event.event == "stderr":
                            stderr_parts.append(base64.b64decode(event.data))
                        elif event.event == "exit":
                            exit_data = json.loads(event.data)
                            return ExecResult(
                                stdout=b"".join(stdout_parts),
                                stderr=b"".join(stderr_parts),
                                exit_code=int(exit_data["exit_code"]),
                            )
                        elif event.event == "error":
                            err_data = json.loads(event.data)
                            raise ExecTransportError(
                                command=tuple(argv),
                                cause=Exception(err_data.get("error", "unknown error")),
                            )

                for line in line_decoder.flush():
                    event = sse_decoder.decode(line)
                    if event is None:
                        continue
                    if event.event == "stdout":
                        stdout_parts.append(base64.b64decode(event.data))
                    elif event.event == "stderr":
                        stderr_parts.append(base64.b64decode(event.data))
                    elif event.event == "exit":
                        exit_data = json.loads(event.data)
                        return ExecResult(
                            stdout=b"".join(stdout_parts),
                            stderr=b"".join(stderr_parts),
                            exit_code=int(exit_data["exit_code"]),
                        )
                    elif event.event == "error":
                        err_data = json.loads(event.data)
                        raise ExecTransportError(
                            command=tuple(argv),
                            cause=Exception(err_data.get("error", "unknown error")),
                        )

                raise ExecTransportError(
                    command=tuple(argv),
                    cause=Exception("SSE stream ended without exit event"),
                )

        except asyncio.TimeoutError as e:
            raise ExecTimeoutError(command=tuple(argv), timeout_s=effective_timeout, cause=e) from e
        except (ExecTimeoutError, ExecTransportError):
            raise
        except aiohttp.ClientError as e:
            raise ExecTransportError(command=tuple(argv), cause=e) from e
        except Exception as e:
            raise ExecTransportError(command=tuple(argv), cause=e) from e

    def supports_pty(self) -> bool:
        return True

    async def _pump_ws_output(self, entry: _CloudflarePtyProcessEntry) -> None:
        try:
            while True:
                msg = await entry.ws.receive()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    async with entry.output_lock:
                        entry.output_chunks.append(msg.data)
                    entry.output_notify.set()
                    continue
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.debug("Ignoring non-JSON PTY text frame: %s", msg.data)
                        continue

                    msg_type = payload.get("type")
                    if msg_type == "ready":
                        continue
                    if msg_type == "exit":
                        code = payload.get("code")
                        entry.exit_code = code if isinstance(code, int) else None
                        entry.output_closed.set()
                        entry.output_notify.set()
                        break
                    if msg_type == "error":
                        logger.warning("Cloudflare PTY error frame: %s", payload.get("message"))
                        entry.output_closed.set()
                        entry.output_notify.set()
                        break
                    continue
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    entry.output_closed.set()
                    entry.output_notify.set()
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Cloudflare PTY pump ended with an exception", exc_info=True)
            entry.output_closed.set()
            entry.output_notify.set()

    async def _collect_pty_output(
        self,
        *,
        entry: _CloudflarePtyProcessEntry,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[bytes, int | None]:
        deadline = time.monotonic() + (yield_time_ms / 1000)
        output = bytearray()

        while True:
            async with entry.output_lock:
                while entry.output_chunks:
                    output.extend(entry.output_chunks.popleft())

            if entry.output_closed.is_set():
                async with entry.output_lock:
                    while entry.output_chunks:
                        output.extend(entry.output_chunks.popleft())
                break

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break

            try:
                await asyncio.wait_for(entry.output_notify.wait(), timeout=remaining_s)
            except asyncio.TimeoutError:
                break
            entry.output_notify.clear()

        text = output.decode("utf-8", errors="replace")
        truncated_text, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
        return truncated_text.encode("utf-8", errors="replace"), original_token_count

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _CloudflarePtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code = entry.exit_code if entry.output_closed.is_set() else None
        live_process_id: int | None = process_id
        if entry.output_closed.is_set():
            async with self._pty_lock:
                removed = self._pty_processes.pop(process_id, None)
                self._reserved_pty_process_ids.discard(process_id)
            if removed is not None:
                await self._terminate_pty_entry(removed)
            live_process_id = None

        return PtyExecUpdate(
            process_id=live_process_id,
            output=output,
            exit_code=exit_code,
            original_token_count=original_token_count,
        )

    async def _prune_pty_processes_if_needed(self) -> _CloudflarePtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None

        meta = [
            (process_id, entry.last_used, entry.output_closed.is_set())
            for process_id, entry in self._pty_processes.items()
        ]
        process_id_to_prune = process_id_to_prune_from_meta(meta)
        if process_id_to_prune is None:
            return None

        self._reserved_pty_process_ids.discard(process_id_to_prune)
        return self._pty_processes.pop(process_id_to_prune, None)

    async def _terminate_pty_entry(self, entry: _CloudflarePtyProcessEntry) -> None:
        with suppress(Exception):
            await entry.ws.close()
        if entry.pump_task is None:
            return
        entry.pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await entry.pump_task

    async def _cleanup_unregistered_pty(
        self,
        entry: _CloudflarePtyProcessEntry | None,
        ws: aiohttp.ClientWebSocketResponse | None,
        registered: bool,
    ) -> None:
        """Best-effort cleanup of a PTY WebSocket or entry that was never registered."""
        if entry is not None and not registered:
            await self._terminate_pty_entry(entry)
        elif ws is not None and not registered:
            with suppress(Exception):
                await ws.close()

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
        _ = timeout
        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        command_text = shlex.join(str(part) for part in sanitized_command)

        ws: aiohttp.ClientWebSocketResponse | None = None
        entry: _CloudflarePtyProcessEntry | None = None
        registered = False
        pruned_entry: _CloudflarePtyProcessEntry | None = None
        process_id = 0
        process_count = 0

        try:
            ws = await self._session().ws_connect(self._ws_pty_url())

            ready_deadline = time.monotonic() + 30.0
            while True:
                remaining_s = ready_deadline - time.monotonic()
                if remaining_s <= 0:
                    raise asyncio.TimeoutError()

                msg = await asyncio.wait_for(ws.receive(), timeout=remaining_s)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "ready":
                        break
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    continue
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise ExecTransportError(
                        command=tuple(str(part) for part in command),
                        cause=Exception("WebSocket closed before PTY ready"),
                    )

            entry = _CloudflarePtyProcessEntry(ws=ws, tty=tty)
            entry.pump_task = asyncio.create_task(self._pump_ws_output(entry))
            await ws.send_bytes(f"{command_text}\n".encode())

            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned_entry = await self._prune_pty_processes_if_needed()
                self._pty_processes[process_id] = entry
                registered = True
                process_count = len(self._pty_processes)
        except asyncio.TimeoutError as e:
            await self._cleanup_unregistered_pty(entry, ws, registered)
            raise ExecTimeoutError(
                command=tuple(str(part) for part in command),
                timeout_s=30.0,
                cause=e,
            ) from e
        except asyncio.CancelledError:
            await self._cleanup_unregistered_pty(entry, ws, registered)
            raise
        except ExecTransportError:
            await self._cleanup_unregistered_pty(entry, ws, registered)
            raise
        except Exception as e:
            await self._cleanup_unregistered_pty(entry, ws, registered)
            raise ExecTransportError(command=tuple(str(part) for part in command), cause=e) from e

        if pruned_entry is not None:
            await self._terminate_pty_entry(pruned_entry)

        if process_count >= PTY_PROCESSES_WARNING:
            logger.warning(
                "PTY process count reached warning threshold: %s active sessions",
                process_count,
            )

        yield_time_ms = 10_000 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=clamp_pty_yield_time_ms(yield_time_ms),
            max_output_tokens=max_output_tokens,
        )
        return await self._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        async with self._pty_lock:
            entry = self._resolve_pty_session_entry(
                pty_processes=self._pty_processes,
                session_id=session_id,
            )

        if chars:
            if not entry.tty:
                raise RuntimeError("stdin is not available for this process")
            await entry.ws.send_bytes(chars.encode("utf-8"))
            await asyncio.sleep(0.1)

        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=resolve_pty_write_yield_time_ms(
                yield_time_ms=yield_time_ms,
                input_empty=chars == "",
            ),
            max_output_tokens=max_output_tokens,
        )
        entry.last_used = time.monotonic()
        return await self._finalize_pty_update(
            process_id=session_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_terminate_all(self) -> None:
        async with self._pty_lock:
            entries = list(self._pty_processes.values())
            self._pty_processes.clear()
            self._reserved_pty_process_ids.clear()

        for entry in entries:
            await self._terminate_pty_entry(entry)

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            await self._check_read_with_exec(path, user=user)

        workspace_path = await self._validate_path_access(path)
        http = self._session()
        url_path = quote(sandbox_path_str(workspace_path).lstrip("/"), safe="/")
        url = self._url(f"file/{url_path}")

        try:
            async with http.get(url, timeout=self._request_timeout()) as resp:
                if resp.status == 404:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceReadNotFoundError(
                        path=workspace_path,
                        context={"message": body.get("error", "not found")},
                    )
                if resp.status == 403:
                    body = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceArchiveReadError(
                        path=workspace_path,
                        context={
                            "reason": "path_escape",
                            "http_status": resp.status,
                            "message": body.get("error", "path escapes /workspace"),
                        },
                    )
                if resp.status != 200:
                    body = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceArchiveReadError(
                        path=workspace_path,
                        context={
                            "reason": "http_error",
                            "http_status": resp.status,
                            "message": body.get("error", f"HTTP {resp.status}"),
                        },
                    )
                return io.BytesIO(self._decode_streamed_payload(await resp.read()))
        except (WorkspaceReadNotFoundError, WorkspaceArchiveReadError):
            raise
        except aiohttp.ClientError as e:
            raise WorkspaceArchiveReadError(path=workspace_path, cause=e) from e
        except Exception as e:
            raise WorkspaceArchiveReadError(path=workspace_path, cause=e) from e

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_write_with_exec(path, user=user)

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)

        payload_bytes = bytes(payload)
        workspace_path = await self._validate_path_access(path, for_write=True)

        http = self._session()
        url_path = quote(sandbox_path_str(workspace_path).lstrip("/"), safe="/")
        url = self._url(f"file/{url_path}")

        try:
            async with http.put(
                url,
                data=payload_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=self._request_timeout(),
            ) as resp:
                if resp.status == 403:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceArchiveWriteError(
                        path=workspace_path,
                        context={
                            "reason": "path_escape",
                            "http_status": resp.status,
                            "message": body.get("error", "path escapes /workspace"),
                        },
                    )
                if resp.status != 200:
                    body = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceArchiveWriteError(
                        path=workspace_path,
                        context={
                            "reason": "http_error",
                            "http_status": resp.status,
                            "message": body.get("error", f"HTTP {resp.status}"),
                        },
                    )
        except WorkspaceArchiveWriteError:
            raise
        except aiohttp.ClientError as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        http = self._session()
        url = self._url("running")
        try:
            async with http.get(url, timeout=self._request_timeout()) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return bool(data.get("running", False))
        except Exception:
            return False

    @retry_async(
        retry_if=lambda exc, self: isinstance(exc, aiohttp.ClientError)
        or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        or _is_transient_workspace_error(exc)
    )
    async def _persist_workspace_via_http(self) -> io.IOBase:
        root = self._workspace_root_path()
        skip = self._persist_workspace_skip_relpaths()
        excludes_param = ",".join(
            rel.as_posix().removeprefix("./")
            for rel in sorted(skip, key=lambda rel: rel.as_posix())
        )
        params: dict[str, str] = {}
        if excludes_param:
            params["excludes"] = excludes_param

        http = self._session()
        url = self._url("persist")
        try:
            async with http.post(url, params=params, timeout=self._request_timeout()) as resp:
                if resp.status != 200:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceArchiveReadError(
                        path=root,
                        context={
                            "reason": "http_error",
                            "http_status": resp.status,
                            "message": body.get("error", f"HTTP {resp.status}"),
                        },
                    )
                return io.BytesIO(self._decode_streamed_payload(await resp.read()))
        except WorkspaceArchiveReadError:
            raise
        except aiohttp.ClientError as e:
            raise WorkspaceArchiveReadError(path=root, cause=e) from e
        except Exception as e:
            raise WorkspaceArchiveReadError(path=root, cause=e) from e

    @retry_async(
        retry_if=lambda exc, self, data: isinstance(exc, aiohttp.ClientError)
        or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        or _is_transient_workspace_error(exc)
    )
    async def _hydrate_workspace_via_http(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceArchiveWriteError(path=root, context={"reason": "non_bytes_payload"})

        try:
            validate_tar_bytes(bytes(raw))
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": e.member,
                    "detail": str(e),
                },
                cause=e,
            ) from e

        http = self._session()
        url = self._url("hydrate")
        try:
            async with http.post(
                url,
                data=bytes(raw),
                headers={"Content-Type": "application/octet-stream"},
                timeout=self._request_timeout(),
            ) as resp:
                if resp.status != 200:
                    body: dict[str, Any] = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    raise WorkspaceArchiveWriteError(
                        path=root,
                        context={
                            "reason": "http_error",
                            "http_status": resp.status,
                            "message": body.get("error", f"HTTP {resp.status}"),
                        },
                    )
        except WorkspaceArchiveWriteError:
            raise
        except aiohttp.ClientError as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e

    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        return await with_ephemeral_mounts_removed(
            self,
            self._persist_workspace_via_http,
            error_path=root,
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key="snapshot_error_before_remount_corruption",
        )

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        await with_ephemeral_mounts_removed(
            self,
            lambda: self._hydrate_workspace_via_http(data),
            error_path=root,
            error_cls=WorkspaceArchiveWriteError,
            operation_error_context_key="hydrate_error_before_remount_corruption",
        )


class CloudflareSandboxClient(BaseSandboxClient[CloudflareSandboxClientOptions]):
    """Cloudflare Sandbox Service backed sandbox client."""

    backend_id = "cloudflare"
    _instrumentation: Instrumentation
    _exec_timeout_s: float
    _request_timeout_s: float

    def __init__(
        self,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
        exec_timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
        request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies
        self._exec_timeout_s = exec_timeout_s
        self._request_timeout_s = request_timeout_s

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: CloudflareSandboxClientOptions,
    ) -> SandboxSession:
        if not options.worker_url:
            raise ConfigurationError(
                message="CloudflareSandboxClientOptions.worker_url must not be empty",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id},
            )

        if manifest is None:
            manifest = Manifest()
        if manifest.root != "/workspace":
            raise ConfigurationError(
                message=(
                    "Cloudflare sandboxes only support manifest.root='/workspace' "
                    "because persistence and hydration are fixed to /workspace"
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id, "manifest_root": manifest.root},
            )

        # Resolve API key for auth.
        api_key = options.api_key or os.environ.get("CLOUDFLARE_SANDBOX_API_KEY")

        # Get a server-generated sandbox ID from the Cloudflare Sandbox Service.
        sandbox_id = await self._request_sandbox_id(
            options.worker_url, api_key, request_timeout_s=self._request_timeout_s
        )

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = CloudflareSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            worker_url=options.worker_url.rstrip("/"),
            sandbox_id=sandbox_id,
            exposed_ports=options.exposed_ports,
        )
        inner = CloudflareSandboxSession(
            state=state,
            api_key=api_key,
            exec_timeout_s=self._exec_timeout_s,
            request_timeout_s=self._request_timeout_s,
        )
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, CloudflareSandboxSession):
            raise TypeError("CloudflareSandboxClient.delete expects a CloudflareSandboxSession")
        await inner.shutdown()
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, CloudflareSandboxSessionState):
            raise TypeError(
                "CloudflareSandboxClient.resume expects a CloudflareSandboxSessionState"
            )
        inner = CloudflareSandboxSession.from_state(
            state,
            exec_timeout_s=self._exec_timeout_s,
            request_timeout_s=self._request_timeout_s,
        )
        reconnected = await inner.running()
        if not reconnected:
            state.workspace_root_ready = False
        inner._set_start_state_preserved(reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return CloudflareSandboxSessionState.model_validate(payload)

    async def _request_sandbox_id(
        self,
        worker_url: str,
        api_key: str | None,
        *,
        request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
    ) -> str:
        """Request a sandbox ID from the Cloudflare Sandbox Service via ``POST /sandbox``."""
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = f"{worker_url.rstrip('/')}/v1/sandbox"
        try:
            async with aiohttp.ClientSession(headers=headers) as http:
                async with http.post(
                    url, timeout=aiohttp.ClientTimeout(total=request_timeout_s)
                ) as resp:
                    if resp.status != 200:
                        body: dict[str, Any] = {}
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            pass
                        raise ConfigurationError(
                            message=(
                                f"POST /sandbox failed: {body.get('error', f'HTTP {resp.status}')}"
                            ),
                            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                            op="start",
                            context={"http_status": resp.status},
                        )
                    data = await resp.json()
                    sandbox_id = data.get("id")
                    if not isinstance(sandbox_id, str) or not sandbox_id:
                        raise ConfigurationError(
                            message="POST /sandbox returned invalid id",
                            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                            op="start",
                            context={},
                        )
                    return sandbox_id
        except ConfigurationError:
            raise
        except aiohttp.ClientError as e:
            raise ConfigurationError(
                message=f"POST /sandbox request failed: {e}",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"cause_type": type(e).__name__},
            ) from e


__all__ = [
    "CloudflareSandboxClient",
    "CloudflareSandboxClientOptions",
    "CloudflareSandboxSession",
    "CloudflareSandboxSessionState",
]
