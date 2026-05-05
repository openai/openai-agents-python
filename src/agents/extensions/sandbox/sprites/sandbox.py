"""Sprites sandbox (https://sprites.dev) implementation.

Create a Sprites organization, set ``SPRITES_API_TOKEN``, and optionally
``SPRITES_API_URL`` (defaults to ``https://api.sprites.dev``).

This module provides a Sprites-backed sandbox client/session that delegates to
the ``sprites-py`` SDK. Exec runs over the multiplexed control-plane WebSocket
(``ControlConnection`` / ``OpConn``) directly so cancellation, timeout, and
streaming work cleanly with the agents-python event loop. Short, non-streaming
lifecycle calls (``create_sprite``, ``get_sprite``, ``delete_sprite``,
filesystem read/write) are wrapped in ``asyncio.to_thread`` because the
upstream SDK exposes them synchronously.

The ``sprites-py`` dependency is intended to be optional (installed via the
``[sprites]`` extra), so package-level exports guard imports of this module.
Within this module the upstream SDK is imported normally so IDEs can resolve
and navigate types.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import posixpath
import shlex
import tarfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import sprites
from sprites import Sprite, SpritesClient
from sprites.control import (
    ControlConnection,
    OpConn,
    get_control_connection,
    release_control_connection,
)
from sprites.exceptions import (
    AuthenticationError,
    FileNotFoundError_,
    NetworkError,
    NotFoundError,
    SpriteError,
)
from sprites.types import URLSettings

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
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
from ....sandbox.session.runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    RuntimeHelperScript,
)
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tarfile
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

WorkspacePersistenceMode = Literal["tar"]
"""Workspace persistence modes supported by the Sprites sandbox.

Only ``"tar"`` is supported in v1; native sprite checkpoints are tracked as a
follow-up because their iterator-based streaming API needs a separate async
wrapper.
"""

UrlAuth = Literal["sprite", "public"]

DEFAULT_SPRITES_API_URL = "https://api.sprites.dev"
DEFAULT_SPRITES_WORKSPACE_ROOT = "/workspace"
DEFAULT_SPRITES_WAIT_FOR_RUNNING_TIMEOUT_S = 45.0
DEFAULT_SPRITES_IDLE_CLOSE_SECONDS = 60.0
"""Default idle threshold after which control connections are closed so the
sprite can drop back to ``warm`` and stop accruing running-state cost. The
next I/O reopens a control connection; the platform auto-wakes the sprite on
traffic arrival, so the cost is just the WS reconnect (~1s)."""

# The upstream sprite status enum is not exported from sprites-py; values are
# defined by the API. A sprite that has finished provisioning reports either
# ``"warm"`` (VM is up, idle, ready to accept requests) or ``"running"``
# (actively handling HTTP traffic). Both are valid for our purposes — exec and
# filesystem operations succeed as soon as the sprite is warm; the platform
# transitions warm → running automatically when traffic arrives.
_SPRITE_READY_STATUSES = frozenset({"warm", "running"})
_SPRITE_READY_POLL_INTERVAL_S = 1.0
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)

# Stdout sentinels used by ``hydrate_workspace`` to detect a partial tar
# extract. Until sprite-env's WS layer reliably round-trips exit codes,
# ``ExecResult.exit_code`` cannot be the only signal — a remote failure may
# still surface as exit 0 over the wire. Anchoring the decision on stdout
# (which the local shell controls before the WS hop) closes that gap.
_HYDRATE_OK_SENTINEL = "__SPRITES_HYDRATE_OK__"
_HYDRATE_FAIL_SENTINEL = "__SPRITES_HYDRATE_FAIL__"

logger = logging.getLogger(__name__)


def _resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    """Pin a Sprites-specific workspace root when the manifest uses the framework default."""

    if manifest is None:
        return Manifest(root=DEFAULT_SPRITES_WORKSPACE_ROOT)
    if manifest.root == _DEFAULT_MANIFEST_ROOT:
        return manifest.model_copy(update={"root": DEFAULT_SPRITES_WORKSPACE_ROOT})
    return manifest


@dataclass
class _SpritePtyProcessEntry:
    """Tracks an in-flight PTY operation for ``SpritesSandboxSession``."""

    op_conn: OpConn
    control: ControlConnection
    tty: bool
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    last_used: float = field(default_factory=time.monotonic)


def _validate_tar_bytes(raw: bytes) -> None:
    """Validate that ``raw`` is a safe tar archive before extraction."""

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            validate_tarfile(tar)
    except UnsafeTarMemberError as exc:
        raise ValueError(str(exc)) from exc
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("invalid tar stream") from exc


class SpritesSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Sprites sandbox backend.

    Field order is part of the v1 public API (pinned by
    ``tests/sandbox/test_compatibility_guards.py``); future fields must be
    appended.
    """

    type: Literal["sprites"] = "sprites"
    sprite_name: str | None = None
    """Existing sprite to attach to. When ``None`` (default), a fresh sprite is
    created and deleted at session shutdown."""

    url_auth: UrlAuth = "sprite"
    """URL auth mode for the sprite. ``"sprite"`` restricts access to
    organization members (default); ``"public"`` exposes the sprite URL to the
    public internet."""

    ram_mb: int | None = None
    cpus: int | None = None
    region: str | None = None
    storage_gb: int | None = None
    """Optional sprite ``SpriteConfig`` knobs. Ignored when attaching to an
    existing sprite."""

    exposed_ports: tuple[int, ...] = ()
    """Ports expected to be exposed by services declared in the sprite image.
    Sprites supports at most one externally routable port per sprite, so this
    tuple may have at most one entry."""

    env: dict[str, str] | None = None
    """Reserved for future per-session environment overrides; not yet wired
    through to the sprite create call by ``sprites-py``."""

    timeout_ms: int | None = None
    """Reserved for future sprite-side idle timeout configuration."""

    wait_for_running_timeout_s: float = DEFAULT_SPRITES_WAIT_FOR_RUNNING_TIMEOUT_S
    """How long to poll ``get_sprite`` waiting for the sprite to reach
    ``running`` status before raising ``WorkspaceStartError``."""

    workspace_persistence: WorkspacePersistenceMode = "tar"
    """Workspace persistence mode. v1 supports only ``"tar"``."""

    idle_close_seconds: float = DEFAULT_SPRITES_IDLE_CLOSE_SECONDS
    """Seconds of inactivity after which the session closes its control
    connections so the sprite can drop back to ``warm``. Set to ``0`` (or
    any negative value) to disable — connections stay open until shutdown.
    Default ``60.0`` matches Sprites' running-state idle billing window."""

    def __init__(
        self,
        sprite_name: str | None = None,
        url_auth: UrlAuth = "sprite",
        ram_mb: int | None = None,
        cpus: int | None = None,
        region: str | None = None,
        storage_gb: int | None = None,
        exposed_ports: tuple[int, ...] = (),
        env: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        wait_for_running_timeout_s: float = DEFAULT_SPRITES_WAIT_FOR_RUNNING_TIMEOUT_S,
        workspace_persistence: WorkspacePersistenceMode = "tar",
        idle_close_seconds: float = DEFAULT_SPRITES_IDLE_CLOSE_SECONDS,
        *,
        type: Literal["sprites"] = "sprites",
    ) -> None:
        super().__init__(
            type=type,
            sprite_name=sprite_name,
            url_auth=url_auth,
            ram_mb=ram_mb,
            cpus=cpus,
            region=region,
            storage_gb=storage_gb,
            exposed_ports=exposed_ports,
            env=env,
            timeout_ms=timeout_ms,
            wait_for_running_timeout_s=wait_for_running_timeout_s,
            workspace_persistence=workspace_persistence,
            idle_close_seconds=idle_close_seconds,
        )


class SpritesSandboxSessionState(SandboxSessionState):
    """Serializable state for a Sprites-backed session.

    ``token`` and ``base_url`` are intentionally absent — ``resume()`` reads
    them from the live ``SpritesSandboxClient`` instead, matching the
    token-non-leakage contract documented for the Vercel provider.
    """

    type: Literal["sprites"] = "sprites"
    sprite_name: str
    created_by_us: bool = True
    url_auth: UrlAuth = "sprite"
    ram_mb: int | None = None
    cpus: int | None = None
    region: str | None = None
    storage_gb: int | None = None
    env: dict[str, str] | None = None
    timeout_ms: int | None = None
    workspace_persistence: WorkspacePersistenceMode = "tar"
    idle_close_seconds: float = DEFAULT_SPRITES_IDLE_CLOSE_SECONDS
    wait_for_running_timeout_s: float = DEFAULT_SPRITES_WAIT_FOR_RUNNING_TIMEOUT_S


class SpritesSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by a Sprites sprite."""

    state: SpritesSandboxSessionState
    _client: SpritesClient | None
    _sprite: Sprite | None
    _control: ControlConnection | None
    _token: str | None
    _base_url: str
    _pty_lock: asyncio.Lock
    _pty_processes: dict[int, _SpritePtyProcessEntry]
    _reserved_pty_process_ids: set[int]
    _warmth_verified: bool
    _last_activity_at: float
    _idle_close_seconds: float
    _idle_watch_task: asyncio.Task[None] | None
    _inflight_op_count: int

    def __init__(
        self,
        *,
        state: SpritesSandboxSessionState,
        token: str | None = None,
        base_url: str = DEFAULT_SPRITES_API_URL,
        client: SpritesClient | None = None,
        sprite: Sprite | None = None,
    ) -> None:
        self.state = state
        self._token = token
        self._base_url = base_url
        self._client = client
        self._sprite = sprite
        self._control = None
        self._pty_lock = asyncio.Lock()
        self._pty_processes = {}
        self._reserved_pty_process_ids = set()
        self._warmth_verified = False
        # Idle-close: when an I/O operation hasn't run for ``idle_close_seconds``,
        # the watcher closes the control-connection pool so the sprite can drop
        # to ``warm`` and stop accruing running-state cost. The next I/O
        # operation reopens a connection; the platform auto-wakes the sprite on
        # traffic arrival.
        self._last_activity_at = time.monotonic()
        self._idle_close_seconds = float(state.idle_close_seconds)
        self._idle_watch_task = None
        # Tracks non-PTY exec/read/write operations currently using the control
        # connection. The idle watcher must skip closure while any are in-flight
        # so a long-running command (e.g. ``apt-get install``) is not cut off
        # mid-execution when it crosses the idle threshold.
        self._inflight_op_count = 0

    @classmethod
    def from_state(
        cls,
        state: SpritesSandboxSessionState,
        *,
        token: str | None = None,
        base_url: str = DEFAULT_SPRITES_API_URL,
        client: SpritesClient | None = None,
        sprite: Sprite | None = None,
    ) -> SpritesSandboxSession:
        return cls(state=state, token=token, base_url=base_url, client=client, sprite=sprite)

    def supports_pty(self) -> bool:
        return True

    # ----- internal helpers -----

    def _ensure_client_sync(self) -> SpritesClient:
        client = self._client
        if client is not None:
            return client
        if not self._token:
            raise ConfigurationError(
                message=(
                    "SpritesSandboxSession requires a Sprites API token "
                    "(set SPRITES_API_TOKEN or pass token=... to SpritesSandboxClient)"
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": "sprites"},
            )
        client = SpritesClient(token=self._token, base_url=self._base_url, control_mode=True)
        self._client = client
        return client

    async def _ensure_sprite(self) -> Sprite:
        existing = self._sprite
        if existing is not None:
            return existing

        client = self._ensure_client_sync()
        sprite: Sprite
        if self.state.created_by_us:
            # Provision a fresh sprite. ``create_sprite`` raises eagerly if the
            # platform rejects the request, so we still surface creation
            # failures synchronously here.
            config = self._build_sprite_config()
            try:
                sprite = await asyncio.to_thread(
                    client.create_sprite, self.state.sprite_name, config
                )
            except (NetworkError, AuthenticationError, NotFoundError, SpriteError) as exc:
                raise WorkspaceStartError(
                    path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "reason": "create_failed",
                    },
                    cause=exc,
                ) from exc
            await self._maybe_update_url_settings(sprite)
            self._sprite = sprite
            return sprite

        # Named-attach: just construct the handle.
        sprite = await asyncio.to_thread(client.sprite, self.state.sprite_name)
        self._sprite = sprite
        return sprite

    async def _try_attach_existing_sprite(self) -> Sprite | None:
        """Look up the recorded sprite name and bind it without provisioning.

        Used by ``resume()`` for both ``created_by_us=True`` and named-attach
        sessions to differentiate "the original sprite is still there" from
        "the original is gone and we need to fall through to a fresh create".
        Returns ``None`` only when the platform reports the sprite missing —
        any other error propagates so the caller can decide whether to retry
        or surface the failure.
        """

        if self._sprite is not None:
            return self._sprite

        client = self._ensure_client_sync()
        try:
            sprite: Sprite = await asyncio.to_thread(client.get_sprite, self.state.sprite_name)
        except NotFoundError:
            return None
        # Re-bind a Sprite handle with the live status snapshot as ``_sprite``;
        # ``client.sprite(name)`` builds the same handle but doesn't touch the
        # platform, so the get_sprite round-trip above is what proves
        # existence.
        self._sprite = sprite
        return sprite

    # Both ephemeral and named-attach paths now defer the wait-for-running poll
    # (and the URL/org-info refresh that comes with it) until the first I/O
    # operation runs ``_ensure_warm``. The platform auto-wakes paused sprites
    # on traffic arrival and the create POST raises eagerly on rejection, so
    # this purely shifts the warm-up cost from session creation to first use
    # without losing any safety. Callers that need ``Sprite.url`` (e.g.
    # ``_resolve_exposed_port``) call ``_ensure_warm`` themselves.

    async def _ensure_warm(self) -> None:
        """Block until the sprite is ready to accept I/O, but only on first use.

        ``_warmth_verified`` is sticky for the life of the session; cached
        until a transport error invalidates it (e.g., the sprite was deleted
        out from under us and we have to re-attach in a recovery flow).
        """

        self._touch_activity()
        if self._warmth_verified:
            return
        await self._wait_for_sprite_running()
        self._warmth_verified = True

    def _invalidate_warmth(self) -> None:
        """Force the next I/O operation to re-poll the sprite's status."""

        self._warmth_verified = False

    def _touch_activity(self) -> None:
        """Mark this moment as the most recent I/O. Starts the idle watcher
        if it isn't already running."""

        self._last_activity_at = time.monotonic()
        self._maybe_start_idle_watch()

    def _maybe_start_idle_watch(self) -> None:
        if self._idle_close_seconds <= 0:
            return
        task = self._idle_watch_task
        if task is not None and not task.done():
            return
        try:
            self._idle_watch_task = asyncio.create_task(self._idle_watch_loop())
        except RuntimeError:
            # No running event loop (e.g. unit-test fixture creating a session
            # outside an asyncio context). The watcher will start on the next
            # I/O call from inside an active loop.
            self._idle_watch_task = None

    async def _idle_watch_loop(self) -> None:
        # Loop forever, only exiting on cancel. Sleeping for the remaining
        # window each iteration keeps the watcher cheap, and looping (rather
        # than returning after a close-attempt) ensures we stay alive across
        # PTY/active-op skips so the next idle window still gets serviced
        # without depending on subsequent I/O to respawn us.
        try:
            while True:
                elapsed = time.monotonic() - self._last_activity_at
                remaining = self._idle_close_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                closed = await self._close_idle_control_connections()
                if closed:
                    # Connections are closed; nothing to watch until a future
                    # I/O call touches activity and respawns the watcher.
                    return
                # Skipped (PTY active or non-PTY op in flight). Re-check after
                # one idle window — by then the active work may have finished.
                await asyncio.sleep(self._idle_close_seconds)
        except asyncio.CancelledError:
            pass

    async def _close_idle_control_connections(self) -> bool:
        """Close pooled control connections so the sprite can drop to ``warm``.

        Skipped when there are active PTY operations or non-PTY exec/read/write
        operations in flight — those need their connections kept alive.

        Returns ``True`` if connections were closed (or there was nothing to
        close), ``False`` if closure was skipped because work was active.
        """

        if self._pty_processes or self._inflight_op_count > 0:
            return False
        sprite = self._sprite
        if sprite is None:
            return True
        try:
            await sprite.close_control_connection()
        except Exception:
            pass
        return True

    def _build_sprite_config(self) -> sprites.SpriteConfig | None:
        if (
            self.state.ram_mb is None
            and self.state.cpus is None
            and self.state.region is None
            and self.state.storage_gb is None
        ):
            return None
        from sprites.types import SpriteConfig

        return SpriteConfig(
            ram_mb=self.state.ram_mb,
            cpus=self.state.cpus,
            region=self.state.region,
            storage_gb=self.state.storage_gb,
        )

    async def _maybe_update_url_settings(self, sprite: Sprite) -> None:
        # The default URL auth mode set by the API is "sprite"; only call the
        # update endpoint when the user asked for something different to avoid
        # unnecessary round-trips.
        if self.state.url_auth == "sprite":
            return
        try:
            await asyncio.to_thread(
                sprite.update_url_settings, URLSettings(auth=self.state.url_auth)
            )
        except SpriteError:
            # URL auth is best-effort during create; if the platform does not
            # accept the value the user can update it later via the dashboard
            # without breaking the session.
            return

    async def _wait_for_sprite_running(self) -> None:
        client = self._ensure_client_sync()
        deadline_s = max(0.0, float(self.state.wait_for_running_timeout_s)) or float(
            DEFAULT_SPRITES_WAIT_FOR_RUNNING_TIMEOUT_S
        )
        loop = asyncio.get_event_loop()
        start = loop.time()
        last_status: str | None = None
        while True:
            try:
                refreshed = await asyncio.to_thread(client.get_sprite, self.state.sprite_name)
            except NotFoundError as exc:
                raise WorkspaceStartError(
                    path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "reason": "sprite_not_found",
                    },
                    cause=exc,
                ) from exc
            except SpriteError as exc:
                raise WorkspaceStartError(
                    path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "reason": "wait_for_running_failed",
                    },
                    cause=exc,
                ) from exc

            last_status = refreshed.status
            if last_status in _SPRITE_READY_STATUSES:
                self._sprite = refreshed
                return
            if loop.time() - start >= deadline_s:
                raise WorkspaceStartError(
                    path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "reason": "wait_for_running_timeout",
                        "last_status": last_status or "unknown",
                        "timeout_s": deadline_s,
                    },
                )
            await asyncio.sleep(_SPRITE_READY_POLL_INTERVAL_S)

    async def _ensure_control(self) -> ControlConnection:
        sprite = await self._ensure_sprite()
        try:
            return await get_control_connection(sprite)
        except Exception as exc:
            raise ExecTransportError(
                command=("<control_connect>",),
                context={"backend": "sprites", "sprite_name": self.state.sprite_name},
                cause=exc,
            ) from exc

    def _release_control(self, control: ControlConnection) -> None:
        sprite = self._sprite
        if sprite is None:
            return
        try:
            release_control_connection(sprite, control)
        except Exception:
            pass

    def _validate_exposed_ports(self) -> None:
        if len(self.state.exposed_ports) > 1:
            raise ConfigurationError(
                message=(
                    "Sprites supports at most one external exposed port per sprite; "
                    "additional ports must be proxied inside the VM"
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={
                    "backend": "sprites",
                    "exposed_ports": list(self.state.exposed_ports),
                },
            )

    # ----- BaseSandboxSession overrides -----

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _reject_user_arg(self, *, op: Literal["exec", "read", "write"], user: str | User) -> None:
        user_name = user.name if isinstance(user, User) else user
        raise ConfigurationError(
            message=(
                "SpritesSandboxSession does not support sandbox-local users; "
                f"`{op}` must be called without `user`"
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op=op,
            context={"backend": "sprites", "user": user_name},
        )

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        if user is not None:
            self._reject_user_arg(op="exec", user=user)
        return super()._prepare_exec_command(*command, shell=shell, user=user)

    async def _prepare_backend_workspace(self) -> None:
        # Bootstrap: create the workspace root from ``/`` because the workspace
        # directory does not yet exist, and ``_exec_internal`` would otherwise
        # try to ``chdir`` into it.
        root = PurePosixPath(posixpath.normpath(self.state.manifest.root))
        result = await self._exec_with_cwd(
            ["mkdir", "-p", "--", root.as_posix()],
            cwd=None,
            timeout=30.0,
            apply_env=False,
        )
        if not result.ok():
            raise WorkspaceStartError(
                path=posix_path_as_path(root),
                context={
                    "backend": "sprites",
                    "sprite_name": self.state.sprite_name,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )

    async def running(self) -> bool:
        if self._client is None:
            return False
        try:
            refreshed: Sprite = await asyncio.to_thread(
                self._client.get_sprite, self.state.sprite_name
            )
        except Exception:
            return False
        return bool(refreshed.status in _SPRITE_READY_STATUSES)

    async def shutdown(self) -> None:
        # Stop the idle watcher first so it doesn't race with our cleanup.
        watcher = self._idle_watch_task
        if watcher is not None and not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
        self._idle_watch_task = None

        # Tear down any in-flight PTY operations first so their control connections
        # are released back to the pool before the sprite is deleted.
        try:
            await asyncio.wait_for(self.pty_terminate_all(), timeout=2.0)
        except Exception:
            pass

        # Order matters for fast cleanup: delete the sprite FIRST (which kills
        # server-side WebSockets immediately), then close local client state.
        # Otherwise we wait up to ~2s per still-open control connection on the
        # WS close handshake + read-task drain.
        if self.state.created_by_us and self._client is not None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._client.delete_sprite, self.state.sprite_name),
                    timeout=5.0,
                )
            except NotFoundError:
                pass
            except Exception:
                pass

        # Now close local control connections. They'll see ConnectionClosed
        # from the now-deleted sprite and exit fast; cap at 2s as a guardrail.
        if self._sprite is not None:
            try:
                await asyncio.wait_for(self._sprite.close_control_connection(), timeout=2.0)
            except Exception:
                pass
        self._sprite = None

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    async def _resolved_envs(self) -> dict[str, str]:
        """Merge per-session env (from options) with manifest-declared env vars.

        Manifest values win on conflict because they are the more explicit
        configuration surface. Returned dict has only ``str`` values; any
        deferred ``EnvValue`` resolutions are awaited here.
        """

        manifest_envs = await self.state.manifest.environment.resolve()
        session_envs = self.state.env or {}
        return {**session_envs, **manifest_envs}

    @staticmethod
    def _wrap_with_env(command: list[str], envs: dict[str, str]) -> list[str]:
        """Prepend an ``env --`` invocation so the remote process inherits ``envs``.

        Uses argv-form so we don't depend on a shell — the WS exec path runs
        ``execvp`` directly. ``env --`` ensures the following ``NAME=VALUE``
        tokens are parsed as env, not as the program name.
        """

        if not envs:
            return command
        prefix = ["env", "--", *(f"{k}={v}" for k, v in envs.items())]
        return [*prefix, *command]

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        normalized = [str(part) for part in command]
        return await self._exec_with_cwd(normalized, cwd=self.state.manifest.root, timeout=timeout)

    async def _exec_with_cwd(
        self,
        command: list[str],
        *,
        cwd: str | None,
        timeout: float | None,
        apply_env: bool = True,
    ) -> ExecResult:
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        if apply_env:
            envs = await self._resolved_envs()
            if envs:
                normalized = self._wrap_with_env(normalized, envs)

        await self._ensure_warm()

        control: ControlConnection | None = None
        op_conn: OpConn | None = None
        # Mark this op as in-flight so the idle watcher won't close the
        # control connection mid-execution (e.g., long-running ``apt-get
        # install`` from the lazy-mount path crossing ``idle_close_seconds``).
        self._inflight_op_count += 1
        try:
            control = await self._ensure_control()
            try:
                op_conn = await control.start_op(
                    "exec",
                    cmd=list(normalized),
                    dir=cwd,
                    stdin=False,
                )
            except Exception as exc:
                raise ExecTransportError(
                    command=normalized,
                    context={"backend": "sprites", "sprite_name": self.state.sprite_name},
                    cause=exc,
                ) from exc

            try:
                exit_code = await asyncio.wait_for(op_conn.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                # Best-effort: signal the remote process before propagating the timeout.
                try:
                    await op_conn.signal("KILL")
                except Exception:
                    pass
                raise ExecTimeoutError(command=normalized, timeout_s=timeout, cause=exc) from exc

            return ExecResult(
                stdout=op_conn.get_stdout(),
                stderr=op_conn.get_stderr(),
                exit_code=exit_code,
            )
        except (ExecTimeoutError, ExecTransportError):
            raise
        except Exception as exc:
            raise ExecTransportError(
                command=normalized,
                context={"backend": "sprites", "sprite_name": self.state.sprite_name},
                cause=exc,
            ) from exc
        finally:
            self._inflight_op_count -= 1
            self._touch_activity()
            if control is not None:
                self._release_control(control)

    # ----- PTY -----

    def _make_pty_callback(self, entry: _SpritePtyProcessEntry) -> Any:
        # ``OpConn.handle_data`` invokes callbacks synchronously from the read
        # loop running on this event loop, so a sync callback is correct.
        def _callback(payload: bytes) -> None:
            if not payload:
                return
            entry.output_chunks.append(bytes(payload))
            entry.output_notify.set()

        return _callback

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
        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        envs = await self._resolved_envs()
        if envs:
            sanitized_command = self._wrap_with_env(sanitized_command, envs)
        # ``_ensure_control`` will lazily call ``_ensure_sprite``; no extra await here.
        await self._ensure_warm()

        cc: ControlConnection | None = None
        op: OpConn | None = None
        entry: _SpritePtyProcessEntry | None = None
        registered = False
        pruned_entry: _SpritePtyProcessEntry | None = None
        process_id = 0
        process_count = 0

        try:
            cc = await self._ensure_control()
            try:
                op = await cc.start_op(
                    "exec",
                    cmd=list(sanitized_command),
                    dir=self.state.manifest.root,
                    tty=tty,
                    rows=24,
                    cols=80,
                    stdin=True,
                )
            except Exception as exc:
                raise ExecTransportError(
                    command=sanitized_command,
                    context={"backend": "sprites", "sprite_name": self.state.sprite_name},
                    cause=exc,
                ) from exc

            entry = _SpritePtyProcessEntry(op_conn=op, control=cc, tty=tty)
            # Register callbacks before any ``await`` to minimize the start-time
            # race; pre-drain whatever already landed in the OpConn's internal
            # buffers between ``start_op`` returning and this point.
            callback = self._make_pty_callback(entry)
            op.on_stdout = callback
            op.on_stderr = callback
            pre_stdout = op.get_stdout()
            pre_stderr = op.get_stderr()
            if pre_stdout:
                entry.output_chunks.append(pre_stdout)
            if pre_stderr:
                entry.output_chunks.append(pre_stderr)
            if pre_stdout or pre_stderr:
                entry.output_notify.set()

            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned_entry = self._prune_pty_processes_if_needed()
                self._pty_processes[process_id] = entry
                process_count = len(self._pty_processes)
                registered = True
        except asyncio.CancelledError:
            if not registered and entry is not None:
                await self._terminate_pty_entry(entry)
            elif cc is not None:
                self._release_control(cc)
            raise
        except ExecTransportError:
            if cc is not None and (entry is None or not registered):
                self._release_control(cc)
            raise
        except Exception as exc:
            if not registered and entry is not None:
                await self._terminate_pty_entry(entry)
            elif cc is not None:
                self._release_control(cc)
            raise ExecTransportError(
                command=sanitized_command,
                context={"backend": "sprites", "sprite_name": self.state.sprite_name},
                cause=exc,
            ) from exc

        if pruned_entry is not None:
            await self._terminate_pty_entry(pruned_entry)

        if process_count >= PTY_PROCESSES_WARNING:
            logger.warning(
                "Sprites PTY process count reached warning threshold: %s active sessions",
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
            payload = chars.encode("utf-8")
            try:
                await entry.op_conn.write(payload)
            except Exception as exc:
                raise ExecTransportError(
                    command=("<pty_write_stdin>",),
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "session_id": session_id,
                    },
                    cause=exc,
                ) from exc

        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=resolve_pty_write_yield_time_ms(
                yield_time_ms=yield_time_ms, input_empty=chars == ""
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

    async def _collect_pty_output(
        self,
        *,
        entry: _SpritePtyProcessEntry,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[bytes, int | None]:
        deadline = time.monotonic() + (yield_time_ms / 1000)
        output = bytearray()

        while True:
            while entry.output_chunks:
                output.extend(entry.output_chunks.popleft())

            if time.monotonic() >= deadline:
                break

            if self._entry_exit_code(entry) is not None:
                while entry.output_chunks:
                    output.extend(entry.output_chunks.popleft())
                break

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break

            entry.output_notify.clear()
            try:
                await asyncio.wait_for(entry.output_notify.wait(), timeout=remaining_s)
            except asyncio.TimeoutError:
                break

        text = output.decode("utf-8", errors="replace")
        truncated_text, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
        return truncated_text.encode("utf-8", errors="replace"), original_token_count

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _SpritePtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code = self._entry_exit_code(entry)
        live_process_id: int | None = process_id
        if exit_code is not None:
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

    def _prune_pty_processes_if_needed(self) -> _SpritePtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None
        meta: list[tuple[int, float, bool]] = [
            (pid, entry.last_used, self._entry_exit_code(entry) is not None)
            for pid, entry in self._pty_processes.items()
        ]
        target = process_id_to_prune_from_meta(meta)
        if target is None:
            return None
        self._reserved_pty_process_ids.discard(target)
        return self._pty_processes.pop(target, None)

    def _entry_exit_code(self, entry: _SpritePtyProcessEntry) -> int | None:
        op = entry.op_conn
        if not op.is_closed():
            return None
        code = op.get_exit_code()
        # ``OpConn`` initializes ``exit_code`` to -1 and only sets a real value
        # on ``op.complete``. Treat -1 as "not yet known" even if closed (e.g.
        # transport dropped before exit signal arrived).
        if code < 0:
            return None
        return code

    async def _terminate_pty_entry(self, entry: _SpritePtyProcessEntry) -> None:
        op = entry.op_conn
        try:
            if not op.is_closed():
                try:
                    await op.signal("TERM")
                except Exception:
                    pass
                # Brief grace period before forcing.
                for _ in range(5):
                    if op.is_closed():
                        break
                    await asyncio.sleep(0.05)
                if not op.is_closed():
                    try:
                        await op.signal("KILL")
                    except Exception:
                        pass
        finally:
            try:
                op.close()
            except Exception:
                pass
            self._release_control(entry.control)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        await self._ensure_sprite()
        # Make sure the sprite is reachable AND that ``Sprite.url`` /
        # ``organization_name`` are populated — these come from the post-poll
        # ``get_sprite`` refresh.
        await self._ensure_warm()
        sprite = self._sprite
        if sprite is None:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "sprites", "sprite_name": self.state.sprite_name},
            )
        url = sprite.url
        if not url:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "sprites", "sprite_name": self.state.sprite_name},
            )

        # Confirm the requested port is exposed by a service on the sprite. Sprites
        # exposes only one external HTTP port per sprite, so any extra port is a
        # configuration error caught earlier in `_validate_exposed_ports`.
        try:
            services = await asyncio.to_thread(sprite.list_services)
        except SpriteError as exc:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "sprites", "sprite_name": self.state.sprite_name},
                cause=exc,
            ) from exc

        if not any(getattr(service, "http_port", None) == port for service in services):
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="not_configured",
                context={
                    "backend": "sprites",
                    "sprite_name": self.state.sprite_name,
                    "hint": ("declare a service with --http-port=<port> in the sprite image"),
                },
            )

        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "sprites", "sprite_name": self.state.sprite_name, "url": url},
            )
        tls = parsed.scheme == "https"
        return ExposedPortEndpoint(
            host=host,
            port=parsed.port or (443 if tls else 80),
            tls=tls,
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)

        normalized_path = await self._validate_path_access(path)
        sprite = await self._ensure_sprite()
        await self._ensure_warm()
        try:
            payload = await asyncio.to_thread(
                lambda: (sprite.filesystem("/") / sandbox_path_str(normalized_path)).read_bytes()
            )
        except FileNotFoundError_ as exc:
            raise WorkspaceReadNotFoundError(path=normalized_path, cause=exc) from exc
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc
        return io.BytesIO(payload)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            self._reject_user_arg(op="write", user=user)

        normalized_path = await self._validate_path_access(path, for_write=True)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=normalized_path,
                actual_type=type(payload).__name__,
            )

        sprite = await self._ensure_sprite()
        await self._ensure_warm()
        try:
            await asyncio.to_thread(
                lambda: (sprite.filesystem("/") / sandbox_path_str(normalized_path)).write_bytes(
                    bytes(payload)
                )
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        sprite = await self._ensure_sprite()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-{self.state.session_id.hex}.tar")
        )
        excludes = [
            f"--exclude=./{rel_path.as_posix()}"
            for rel_path in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        ]
        tar_command = ("tar", "cf", archive_path.as_posix(), *excludes, ".")
        try:
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.decode("utf-8", errors="replace"),
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
            archive = await asyncio.to_thread(
                lambda: (sprite.filesystem("/") / archive_path.as_posix()).read_bytes()
            )
            return io.BytesIO(archive)
        except WorkspaceArchiveReadError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=root, cause=exc) from exc
        finally:
            try:
                await self.exec("rm", "-f", "--", archive_path.as_posix(), shell=False)
            except Exception:
                pass

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=self._workspace_root_path(),
                actual_type=type(raw).__name__,
            )

        root = self._workspace_root_path()
        sprite = await self._ensure_sprite()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-{self.state.session_id.hex}.tar")
        )

        try:
            _validate_tar_bytes(bytes(raw))
        except ValueError as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc

        try:
            await self.mkdir(root, parents=True)
            await asyncio.to_thread(
                lambda: (sprite.filesystem("/") / archive_path.as_posix()).write_bytes(bytes(raw))
            )
            # Wrap the extract in a stdout-sentinel so a partial extract is
            # detectable even when the WS exit-code wire drops failures (see
            # ``mounts.py`` for the matching pattern). The sentinel runs on
            # the remote shell, so the success/failure decision is driven by
            # the actual ``tar`` exit status, not the round-tripped one.
            extract_script = (
                f"tar xf {shlex.quote(archive_path.as_posix())} "
                f"-C {shlex.quote(root.as_posix())} "
                f"&& printf %s {_HYDRATE_OK_SENTINEL} "
                f"|| (rc=$?; printf %s {_HYDRATE_FAIL_SENTINEL}; exit $rc)"
            )
            result = await self.exec("sh", "-c", extract_script, shell=False)
            stdout_text = result.stdout.decode("utf-8", errors="replace")
            extract_succeeded = _HYDRATE_OK_SENTINEL in stdout_text
            if not extract_succeeded or not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "backend": "sprites",
                        "sprite_name": self.state.sprite_name,
                        "exit_code": result.exit_code,
                        "stdout": stdout_text,
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc
        finally:
            try:
                await self.exec("rm", "-f", "--", archive_path.as_posix(), shell=False)
            except Exception:
                pass


class SpritesSandboxClient(BaseSandboxClient[SpritesSandboxClientOptions]):
    """Sprites-backed sandbox client."""

    backend_id = "sprites"
    _instrumentation: Instrumentation
    _token: str | None
    _base_url: str

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        resolved_token = token if token is not None else os.environ.get("SPRITES_API_TOKEN")
        if not resolved_token:
            raise ConfigurationError(
                message=(
                    "Sprites API token is required. Pass token=... to "
                    "SpritesSandboxClient or set the SPRITES_API_TOKEN environment "
                    "variable."
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": "sprites"},
            )
        self._token = resolved_token
        self._base_url = (
            base_url
            if base_url is not None
            else os.environ.get("SPRITES_API_URL", DEFAULT_SPRITES_API_URL)
        )
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: SpritesSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = _resolve_manifest_root(manifest)
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))

        sprite_name = options.sprite_name or f"openai-agents-{session_id.hex[:12]}"
        created_by_us = options.sprite_name is None

        state = SpritesSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sprite_name=sprite_name,
            created_by_us=created_by_us,
            url_auth=options.url_auth,
            ram_mb=options.ram_mb,
            cpus=options.cpus,
            region=options.region,
            storage_gb=options.storage_gb,
            exposed_ports=options.exposed_ports,
            env=dict(options.env or {}) or None,
            timeout_ms=options.timeout_ms,
            workspace_persistence=options.workspace_persistence,
            idle_close_seconds=options.idle_close_seconds,
            wait_for_running_timeout_s=options.wait_for_running_timeout_s,
        )

        inner = SpritesSandboxSession.from_state(state, token=self._token, base_url=self._base_url)
        inner._validate_exposed_ports()
        await inner._ensure_sprite()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, SpritesSandboxSession):
            raise TypeError("SpritesSandboxClient.delete expects a SpritesSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, SpritesSandboxSessionState):
            raise TypeError("SpritesSandboxClient.resume expects a SpritesSandboxSessionState")

        inner = SpritesSandboxSession.from_state(state, token=self._token, base_url=self._base_url)

        # Always try to reattach to the recorded sprite first, regardless of
        # ``created_by_us``. A successful reattach preserves the live
        # workspace and avoids duplicating resources; only a true reattach
        # warrants ``_set_start_state_preserved(True)``.
        try:
            attached = await inner._try_attach_existing_sprite()
        except (NetworkError, AuthenticationError, SpriteError) as exc:
            # Treat platform errors here as fatal even for ephemeral sessions:
            # we have no signal that the sprite is gone, just that the call
            # failed, and silently recreating risks orphaning the original.
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(state.manifest.root)),
                context={
                    "backend": "sprites",
                    "sprite_name": state.sprite_name,
                    "reason": "reattach_failed",
                },
                cause=exc,
            ) from exc

        if attached is not None:
            inner._set_start_state_preserved(True)
            return self._wrap_session(inner, instrumentation=self._instrumentation)

        # The recorded sprite is gone. Named-attach sessions cannot be
        # silently replaced — the caller asked for a specific sprite by name.
        if not state.created_by_us:
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(state.manifest.root)),
                context={
                    "backend": "sprites",
                    "sprite_name": state.sprite_name,
                    "reason": "sprite_not_found",
                },
            )

        # Ephemeral session whose original sprite was deleted: replace it with
        # a fresh provision. Workspace continuity is lost, so clear the
        # readiness flag and do NOT mark start state as preserved — the
        # session's ``start()`` lifecycle must run a full manifest apply.
        state.workspace_root_ready = False
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return SpritesSandboxSessionState.model_validate(payload)


__all__ = [
    "DEFAULT_SPRITES_API_URL",
    "DEFAULT_SPRITES_WAIT_FOR_RUNNING_TIMEOUT_S",
    "DEFAULT_SPRITES_WORKSPACE_ROOT",
    "SpritesSandboxClient",
    "SpritesSandboxClientOptions",
    "SpritesSandboxSession",
    "SpritesSandboxSessionState",
]
