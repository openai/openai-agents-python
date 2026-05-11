from __future__ import annotations

import asyncio
import copy
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, cast

from ..agent import Agent
from ..run_config import SandboxArchiveLimits, SandboxConcurrencyLimits, SandboxRunConfig
from ..run_context import TContext
from ..run_state import (
    RunState,
    _allocate_unique_agent_identity,
    _build_agent_identity_keys_by_id,
)
from ..tracing import custom_span, get_current_trace
from .capabilities import Capability
from .entries import BaseEntry, Dir, Mount, resolve_workspace_path
from .manifest import Manifest
from .sandbox_agent import SandboxAgent
from .session.base_sandbox_session import BaseSandboxSession
from .session.sandbox_client import BaseSandboxClient
from .session.sandbox_session import SandboxSession
from .session.sandbox_session_state import SandboxSessionState
from .snapshot import NoopSnapshotSpec, SnapshotBase, SnapshotSpec
from .snapshot_defaults import resolve_default_local_snapshot_spec
from .types import User


def _supports_trace_spans() -> bool:
    current_trace = get_current_trace()
    return current_trace is not None and current_trace.export() is not None


class _SandboxSessionResources:
    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        client: BaseSandboxClient[Any] | None,
        owns_session: bool,
    ) -> None:
        self._session = session
        self._client = client
        self._owns_session = owns_session
        self._cleanup_lock = asyncio.Lock()
        self._cleaned = False
        self._started = False

    @property
    def session(self) -> BaseSandboxSession:
        return self._session

    @property
    def state(self) -> SandboxSessionState:
        return self._session.state

    async def ensure_started(self) -> None:
        if self._started and await self._session.running():
            return
        if not self._owns_session and await self._session.running():
            self._started = True
            return
        await self._session.start()
        self._started = True

    async def cleanup(self) -> None:
        if not self._owns_session:
            return
        async with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True

            cleanup_error: BaseException | None = None
            try:
                await self._session.run_pre_stop_hooks()
            except BaseException as exc:  # pragma: no cover
                cleanup_error = exc
            try:
                await self._session.stop()
            except BaseException as exc:  # pragma: no cover
                if cleanup_error is None:
                    cleanup_error = exc
            try:
                await self._session.shutdown()
            except BaseException as exc:  # pragma: no cover
                if cleanup_error is None:
                    cleanup_error = exc
            finally:
                try:
                    if self._client is not None and isinstance(self._session, SandboxSession):
                        await self._client.delete(self._session)
                except BaseException as exc:  # pragma: no cover
                    if cleanup_error is None:
                        cleanup_error = exc
                finally:
                    try:
                        await self._session._aclose_dependencies()
                    except BaseException as exc:  # pragma: no cover
                        if cleanup_error is None:
                            cleanup_error = exc
            if cleanup_error is not None:
                raise cleanup_error


@dataclass
class _SandboxConcurrencyGuard:
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_runs: int = 0


@dataclass(frozen=True)
class _LiveSessionManifestUpdate:
    processed_manifest: Manifest | None
    entries_to_apply: list[tuple[Path, BaseEntry]]


class SandboxRuntimeSessionManager(Generic[TContext]):
    def __init__(
        self,
        *,
        starting_agent: Agent[TContext],
        sandbox_config: SandboxRunConfig | None,
        run_state: RunState[TContext] | None,
    ) -> None:
        self._sandbox_config = sandbox_config
        self._run_state = run_state
        resume_identity_root = starting_agent
        if (
            run_state is not None
            and run_state._starting_agent is not None
            and run_state._current_agent is not None
            and run_state._starting_agent is not run_state._current_agent
        ):
            resume_identity_root = run_state._starting_agent
        self._stable_resume_keys_by_agent_id = _build_agent_identity_keys_by_id(
            resume_identity_root
        )
        self._resources_by_agent: dict[int, _SandboxSessionResources] = {}
        self._current_agent_id: int | None = None
        self._acquired_agents: dict[int, SandboxAgent[TContext]] = {}
        self._resume_keys_by_agent_id: dict[int, str] = {}
        self._resume_source_key_by_agent_id: dict[int, str] = {}
        self._available_resumed_keys_by_name: dict[str, list[str]] | None = None
        self._claimed_resumed_keys: set[str] = set()

    @staticmethod
    def _resume_agent_base_key(agent: Agent[Any]) -> str:
        return agent.name

    @staticmethod
    def _serialize_session_entry(
        *,
        agent: Agent[Any],
        session_state: dict[str, object],
    ) -> dict[str, object]:
        return {
            "agent_name": agent.name,
            "session_state": session_state,
        }

    @property
    def enabled(self) -> bool:
        return self._sandbox_config is not None

    @property
    def current_session(self) -> BaseSandboxSession | None:
        if self._current_agent_id is None:
            return None
        resources = self._resources_by_agent.get(self._current_agent_id)
        if resources is None:
            return None
        return resources.session

    def acquire_agent(self, agent: SandboxAgent[TContext]) -> None:
        agent_id = id(agent)
        if agent_id in self._acquired_agents:
            return

        guard = getattr(agent, "_sandbox_concurrency_guard", None)
        if guard is None:
            guard = _SandboxConcurrencyGuard()
            agent._sandbox_concurrency_guard = guard
        with guard.lock:
            if guard.active_runs > 0:
                raise RuntimeError(
                    f"SandboxAgent {agent.name!r} cannot be reused concurrently across runs"
                )
            guard.active_runs += 1
        self._acquired_agents[agent_id] = agent
        self._ensure_resume_key(agent)

    async def ensure_session(
        self,
        *,
        agent: SandboxAgent[TContext],
        capabilities: list[Capability],
        is_resumed_state: bool,
    ) -> BaseSandboxSession:
        agent_id = id(agent)
        resources = self._resources_by_agent.get(agent_id)
        if resources is None:
            resources = await self._create_resources(
                agent=agent,
                capabilities=capabilities,
                is_resumed_state=is_resumed_state,
            )
            self._resources_by_agent[agent_id] = resources
        self._current_agent_id = agent_id

        await resources.ensure_started()
        return resources.session

    def serialize_resume_state(self) -> dict[str, object] | None:
        existing_payload = (
            copy.deepcopy(self._run_state._sandbox)
            if self._run_state is not None and isinstance(self._run_state._sandbox, dict)
            else None
        )
        if self._sandbox_config is None:
            return existing_payload
        if self._sandbox_config.session is not None:
            return None
        if self._current_agent_id is None:
            return existing_payload
        if self._sandbox_config.client is None:
            return existing_payload
        resources = self._resources_by_agent.get(self._current_agent_id)
        if resources is None:
            return existing_payload

        client = self._resolve_client()
        current_agent = self._acquired_agents.get(self._current_agent_id)
        if current_agent is None:
            return existing_payload

        sessions_by_agent = self._serialize_sessions_by_agent(client)
        return {
            "backend_id": client.backend_id,
            "current_agent_key": self._ensure_resume_key(current_agent),
            "current_agent_name": current_agent.name,
            "session_state": client.serialize_session_state(resources.state),
            "sessions_by_agent": sessions_by_agent,
        }

    async def cleanup(self) -> dict[str, object] | None:
        should_trace_cleanup = bool(self._resources_by_agent)
        span_cm = (
            custom_span(
                "sandbox.cleanup_sessions",
                data={"session_count": len(self._resources_by_agent)},
            )
            if should_trace_cleanup and _supports_trace_spans()
            else nullcontext(None)
        )
        with span_cm:
            cleanup_error: BaseException | None = None
            resume_state: dict[str, object] | None = None
            try:
                for resources in list(self._resources_by_agent.values()):
                    try:
                        await resources.cleanup()
                    except BaseException as exc:  # pragma: no cover
                        if cleanup_error is None:
                            cleanup_error = exc
                if cleanup_error is None:
                    resume_state = self.serialize_resume_state()
            finally:
                self._resources_by_agent.clear()
                self._current_agent_id = None
                self._release_agents()
            if cleanup_error is not None:
                raise cleanup_error
            return resume_state

    async def _create_resources(
        self,
        *,
        agent: SandboxAgent[TContext],
        capabilities: list[Capability],
        is_resumed_state: bool,
    ) -> _SandboxSessionResources:
        sandbox_config = self._require_sandbox_config()
        concurrency_limits = self._resolve_concurrency_limits()
        archive_limits = self._resolve_archive_limits()
        if sandbox_config.session is not None:
            self._configure_session(
                sandbox_config.session,
                concurrency_limits=concurrency_limits,
                archive_limits=archive_limits,
            )
            running = await sandbox_config.session.running()
            manifest_update = self._process_live_session_manifest(
                agent=agent,
                capabilities=capabilities,
                session=sandbox_config.session,
                running=running,
            )
            if manifest_update.entries_to_apply:
                await sandbox_config.session._apply_entry_batch(
                    manifest_update.entries_to_apply,
                    base_dir=sandbox_config.session._manifest_base_dir(),
                )
            if manifest_update.processed_manifest is not None:
                sandbox_config.session.state = sandbox_config.session.state.model_copy(
                    update={"manifest": manifest_update.processed_manifest}
                )
            return _SandboxSessionResources(
                session=sandbox_config.session,
                client=None,
                owns_session=False,
            )

        client = self._resolve_client()
        explicit_state = sandbox_config.session_state
        resume_from_run_state = False
        resumed_payload = self._resume_state_payload_for_agent(
            client=client,
            agent=agent,
            agent_id=id(agent),
        )
        if resumed_payload is not None:
            explicit_state = client.deserialize_session_state(resumed_payload)
            resume_from_run_state = True

        if explicit_state is not None:
            explicit_state = self._process_resumed_state_manifest(
                agent=agent,
                capabilities=capabilities,
                session_state=explicit_state,
            )
            span_cm = (
                custom_span(
                    "sandbox.resume_session",
                    data={"agent_name": agent.name, "backend_id": client.backend_id},
                )
                if _supports_trace_spans()
                else nullcontext(None)
            )
            with span_cm:
                resumed_session = await client.resume(explicit_state)
            self._configure_session(
                resumed_session,
                concurrency_limits=concurrency_limits,
                archive_limits=archive_limits,
            )
            return _SandboxSessionResources(
                session=resumed_session,
                client=client,
                owns_session=True,
            )

        effective_manifest = self._resolve_manifest(
            agent=agent,
            resume_from_run_state=resume_from_run_state,
        )
        run_as_user = self._agent_run_as_user(agent)
        if effective_manifest is not None or run_as_user is not None:
            effective_manifest = self._process_manifest(
                capabilities,
                effective_manifest or Manifest(),
                run_as_user=run_as_user,
            )

        options = sandbox_config.options
        if options is None and not client.supports_default_options:
            raise ValueError(
                "Sandbox execution requires `run_config.sandbox.options` when creating a session"
            )

        span_cm = (
            custom_span(
                "sandbox.create_session",
                data={"agent_name": agent.name, "backend_id": client.backend_id},
            )
            if _supports_trace_spans()
            else nullcontext(None)
        )
        with span_cm:
            session = await client.create(
                snapshot=self._resolve_snapshot_spec(sandbox_config.snapshot),
                manifest=effective_manifest,
                options=options,
            )
        self._configure_session(
            session,
            concurrency_limits=concurrency_limits,
            archive_limits=archive_limits,
        )
        self._ensure_session_manifest_has_run_as_user(session=session, agent=agent)
        return _SandboxSessionResources(session=session, client=client, owns_session=True)

    def _resolve_concurrency_limits(self) -> SandboxConcurrencyLimits:
        sandbox_config = self._require_sandbox_config()
        limits = sandbox_config.concurrency_limits
        limits.validate()
        return limits

    def _resolve_archive_limits(self) -> SandboxArchiveLimits | None:
        sandbox_config = self._require_sandbox_config()
        limits = sandbox_config.archive_limits
        if limits is not None:
            limits.validate()
        return limits

    def _configure_session(
        self,
        session: BaseSandboxSession,
        *,
        concurrency_limits: SandboxConcurrencyLimits,
        archive_limits: SandboxArchiveLimits | None,
    ) -> None:
        session._set_concurrency_limits(concurrency_limits)
        session._set_archive_limits(archive_limits)

    def _resume_state_payload_for_agent(
        self,
        *,
        client: BaseSandboxClient[Any],
        agent: SandboxAgent[TContext],
        agent_id: int,
    ) -> dict[str, object] | None:
        if self._run_state is None or self._run_state._sandbox is None:
            return None

        resumed = self._run_state._sandbox
        backend_id = resumed.get("backend_id")
        if backend_id != client.backend_id:
            raise ValueError(
                "RunState sandbox backend does not match the configured sandbox client"
            )

        sessions_by_agent = resumed.get("sessions_by_agent")
        if isinstance(sessions_by_agent, dict):
            resume_key = self._assign_resumed_agent_key(agent)
            if resume_key is not None:
                payload = self._session_payload_from_entry(sessions_by_agent.get(resume_key))
                if payload is not None:
                    self._remember_resume_source_key(agent_id, resume_key)
                    return payload

            payload = self._session_payload_from_entry(sessions_by_agent.get(str(agent_id)))
            if payload is not None:
                self._remember_resume_source_key(agent_id, str(agent_id))
                return payload

        current_agent_key = resumed.get("current_agent_key")
        current_agent_name = resumed.get("current_agent_name")
        current_agent_id = resumed.get("current_agent_id")
        payload = resumed.get("session_state")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError("RunState sandbox payload is missing `session_state`")
        if isinstance(current_agent_key, str):
            resume_key = self._assign_resumed_agent_key(agent)
            if resume_key != current_agent_key:
                return None
            self._remember_resume_source_key(agent_id, current_agent_key)
            return payload
        if current_agent_name is None and self._run_state._current_agent is not None:
            current_agent_name = self._run_state._current_agent.name
        if isinstance(current_agent_name, str):
            if current_agent_name != self._resume_agent_base_key(agent):
                return None
            self._remember_resume_source_key(agent_id, current_agent_name)
            return payload
        if current_agent_id is None or current_agent_id == agent_id:
            if current_agent_id is not None:
                self._remember_resume_source_key(agent_id, str(current_agent_id))
            return payload
        return None

    def _resolve_client(self) -> BaseSandboxClient[Any]:
        sandbox_config = self._require_sandbox_config()
        if sandbox_config.client is None:
            raise ValueError(
                "Sandbox execution requires `run_config.sandbox.client` "
                "unless a live session is provided"
            )
        return sandbox_config.client

    def _require_sandbox_config(self) -> SandboxRunConfig:
        if self._sandbox_config is None:
            raise ValueError("Sandbox runtime is disabled for this run")
        return self._sandbox_config

    @staticmethod
    def _resolve_snapshot_spec(
        snapshot: SnapshotSpec | SnapshotBase | None,
    ) -> SnapshotSpec | SnapshotBase:
        if snapshot is not None:
            return snapshot
        try:
            return resolve_default_local_snapshot_spec()
        except OSError:
            return NoopSnapshotSpec()

    def _resolve_manifest(
        self,
        *,
        agent: SandboxAgent[TContext],
        resume_from_run_state: bool,
    ) -> Manifest | None:
        sandbox_config = self._require_sandbox_config()
        if sandbox_config.session is not None:
            return cast(Manifest | None, getattr(sandbox_config.session.state, "manifest", None))
        if sandbox_config.session_state is not None:
            return cast(Manifest | None, getattr(sandbox_config.session_state, "manifest", None))
        if resume_from_run_state:
            return None
        if sandbox_config.manifest is not None:
            return sandbox_config.manifest
        return agent.default_manifest

    @staticmethod
    def _process_manifest(
        capabilities: list[Capability],
        manifest: Manifest | None,
        *,
        run_as_user: User | None = None,
    ) -> Manifest | None:
        if manifest is None:
            return None
        processed_manifest = SandboxRuntimeSessionManager._manifest_with_run_as_user(
            manifest.model_copy(deep=True),
            run_as_user,
        )
        for capability in capabilities:
            processed_manifest = capability.process_manifest(processed_manifest)
        return processed_manifest

    @classmethod
    def _process_live_session_manifest(
        cls,
        *,
        agent: SandboxAgent[TContext],
        capabilities: list[Capability],
        session: BaseSandboxSession,
        running: bool,
    ) -> _LiveSessionManifestUpdate:
        current_manifest = session.state.manifest
        processed_manifest = cls._process_manifest(
            capabilities,
            current_manifest,
            run_as_user=cls._agent_run_as_user(agent),
        )
        if processed_manifest is None or processed_manifest == current_manifest:
            return _LiveSessionManifestUpdate(processed_manifest=None, entries_to_apply=[])

        entries_to_apply: list[tuple[Path, BaseEntry]] = []
        if running:
            cls._validate_running_live_session_manifest_update(
                current_manifest=current_manifest,
                processed_manifest=processed_manifest,
            )
            entries_to_apply = cls._diff_live_session_entries(
                current_entries=current_manifest.entries,
                processed_entries=processed_manifest.entries,
            )
            entries_to_apply = [
                (
                    resolve_workspace_path(Path(processed_manifest.root), rel_path),
                    artifact,
                )
                for rel_path, artifact in entries_to_apply
            ]

        return _LiveSessionManifestUpdate(
            processed_manifest=processed_manifest,
            entries_to_apply=entries_to_apply,
        )

    @classmethod
    def _validate_running_live_session_manifest_update(
        cls,
        *,
        current_manifest: Manifest,
        processed_manifest: Manifest,
    ) -> None:
        if processed_manifest.root != current_manifest.root:
            raise ValueError(
                "Running injected sandbox sessions do not support capability changes to "
                "`manifest.root`; use a fresh session or a session_state resume flow."
            )
        if processed_manifest.environment != current_manifest.environment:
            raise ValueError(
                "Running injected sandbox sessions do not support capability changes to "
                "`manifest.environment`; use a fresh session or a session_state resume flow."
            )
        if (
            processed_manifest.users != current_manifest.users
            or processed_manifest.groups != current_manifest.groups
        ):
            raise ValueError(
                "Running injected sandbox sessions do not support capability changes to "
                "`manifest.users` or `manifest.groups`; use a fresh session or a "
                "session_state resume flow."
            )

    @classmethod
    def _diff_live_session_entries(
        cls,
        *,
        current_entries: dict[str | Path, BaseEntry],
        processed_entries: dict[str | Path, BaseEntry],
        parent_rel: Path = Path(),
    ) -> list[tuple[Path, BaseEntry]]:
        current_by_name = {
            Manifest._coerce_rel_path(name): entry for name, entry in current_entries.items()
        }
        processed_by_name = {
            Manifest._coerce_rel_path(name): entry for name, entry in processed_entries.items()
        }

        removed = sorted(current_by_name.keys() - processed_by_name.keys())
        if removed:
            removed_paths = ", ".join((parent_rel / rel).as_posix() for rel in removed)
            raise ValueError(
                "Running injected sandbox sessions do not support removing manifest entries: "
                f"{removed_paths}."
            )

        entries_to_apply: list[tuple[Path, BaseEntry]] = []
        for rel_name, processed_entry in processed_by_name.items():
            rel_path = parent_rel / rel_name
            current_entry = current_by_name.get(rel_name)
            if current_entry is None:
                cls._validate_running_live_session_entry_addition(
                    rel_path=rel_path,
                    entry=processed_entry,
                )
                entries_to_apply.append((rel_path, processed_entry.model_copy(deep=True)))
                continue

            delta_entry = cls._diff_live_session_entry(
                rel_path=rel_path,
                current_entry=current_entry,
                processed_entry=processed_entry,
            )
            if delta_entry is not None:
                entries_to_apply.append((rel_path, delta_entry))

        return entries_to_apply

    @classmethod
    def _diff_live_session_entry(
        cls,
        *,
        rel_path: Path,
        current_entry: BaseEntry,
        processed_entry: BaseEntry,
    ) -> BaseEntry | None:
        if current_entry == processed_entry:
            return None

        if type(current_entry) is not type(processed_entry) or (
            current_entry.is_dir != processed_entry.is_dir
        ):
            raise ValueError(
                "Running injected sandbox sessions do not support replacing manifest entry "
                f"types at {rel_path.as_posix()}; use a fresh session or a session_state "
                "resume flow."
            )

        if isinstance(current_entry, Mount):
            raise ValueError(
                "Running injected sandbox sessions do not support capability changes to mount "
                f"entries at {rel_path.as_posix()}; use a fresh session or a session_state "
                "resume flow."
            )

        if isinstance(current_entry, Dir) and isinstance(processed_entry, Dir):
            changed_children = dict(
                cls._diff_live_session_entries(
                    current_entries=current_entry.children,
                    processed_entries=processed_entry.children,
                    parent_rel=Path(),
                )
            )
            metadata_changed = current_entry.model_dump(
                exclude={"children"}
            ) != processed_entry.model_dump(exclude={"children"})
            if not metadata_changed and not changed_children:
                return None
            return processed_entry.model_copy(update={"children": changed_children}, deep=True)

        return processed_entry.model_copy(deep=True)

    @staticmethod
    def _validate_running_live_session_entry_addition(
        *,
        rel_path: Path,
        entry: BaseEntry,
    ) -> None:
        if SandboxRuntimeSessionManager._entry_contains_mount(entry):
            raise ValueError(
                "Running injected sandbox sessions do not support capability-added mount "
                f"entries at {rel_path.as_posix()}; use a fresh session or a session_state "
                "resume flow."
            )

    @staticmethod
    def _entry_contains_mount(entry: BaseEntry) -> bool:
        if isinstance(entry, Mount):
            return True
        if isinstance(entry, Dir):
            return any(
                SandboxRuntimeSessionManager._entry_contains_mount(child)
                for child in entry.children.values()
            )
        return False

    @classmethod
    def _process_resumed_state_manifest(
        cls,
        *,
        agent: SandboxAgent[TContext],
        capabilities: list[Capability],
        session_state: SandboxSessionState,
    ) -> SandboxSessionState:
        processed_manifest = cls._process_manifest(
            capabilities,
            session_state.manifest,
            run_as_user=cls._agent_run_as_user(agent),
        )
        if processed_manifest is None:
            return session_state
        return session_state.model_copy(update={"manifest": processed_manifest})

    @staticmethod
    def _agent_run_as_user(agent: SandboxAgent[Any]) -> User | None:
        run_as = agent.run_as
        if run_as is None:
            return None
        if isinstance(run_as, User):
            return run_as
        return User(name=run_as)

    @staticmethod
    def _manifest_with_run_as_user(manifest: Manifest, user: User | None) -> Manifest:
        if user is None:
            return manifest
        if any(existing.name == user.name for existing in manifest.users):
            return manifest
        if any(existing.name == user.name for group in manifest.groups for existing in group.users):
            return manifest
        return manifest.model_copy(update={"users": [*manifest.users, user]}, deep=True)

    def _ensure_session_manifest_has_run_as_user(
        self,
        *,
        session: BaseSandboxSession,
        agent: SandboxAgent[TContext],
    ) -> None:
        manifest = session.state.manifest
        processed_manifest = self._manifest_with_run_as_user(
            manifest,
            self._agent_run_as_user(agent),
        )
        if processed_manifest != manifest:
            session.state = session.state.model_copy(update={"manifest": processed_manifest})

    def _release_agents(self) -> None:
        if not self._acquired_agents:
            return

        released = list(self._acquired_agents.values())
        self._acquired_agents.clear()
        self._resume_keys_by_agent_id.clear()
        self._resume_source_key_by_agent_id.clear()
        self._available_resumed_keys_by_name = None
        self._claimed_resumed_keys.clear()
        for agent in released:
            guard = getattr(agent, "_sandbox_concurrency_guard", None)
            if guard is None:
                continue
            with guard.lock:
                guard.active_runs = max(0, guard.active_runs - 1)

    def _ensure_resume_key(self, agent: SandboxAgent[TContext]) -> str:
        agent_id = id(agent)
        existing = self._resume_keys_by_agent_id.get(agent_id)
        if existing is not None:
            return existing

        stable_key = self._stable_resume_key_for_agent(agent)
        if stable_key is not None and stable_key not in self._used_resume_keys():
            self._resume_keys_by_agent_id[agent_id] = stable_key
            return stable_key

        resumed_key = self._assign_resumed_agent_key(agent)
        if resumed_key is not None:
            return resumed_key

        key = _allocate_unique_agent_identity(
            self._resume_agent_base_key(agent),
            self._used_resume_keys(),
        )
        self._resume_keys_by_agent_id[agent_id] = key
        return key

    def _stable_resume_key_for_agent(self, agent: Agent[Any]) -> str | None:
        return self._stable_resume_keys_by_agent_id.get(id(agent))

    def _assign_resumed_agent_key(self, agent: SandboxAgent[TContext]) -> str | None:
        agent_id = id(agent)
        existing = self._resume_keys_by_agent_id.get(agent_id)
        if existing is not None:
            return existing
        if self._run_state is None or self._run_state._sandbox is None:
            return None

        resumed = self._run_state._sandbox
        current_key = resumed.get("current_agent_key")
        stable_key = self._stable_resume_key_for_agent(agent)
        sessions_by_agent = resumed.get("sessions_by_agent")
        if (
            isinstance(stable_key, str)
            and stable_key not in self._claimed_resumed_keys
            and self._entry_matches_agent_name(sessions_by_agent, stable_key, agent.name)
        ):
            self._claimed_resumed_keys.add(stable_key)
            self._resume_keys_by_agent_id[agent_id] = stable_key
            return stable_key

        base = self._resume_agent_base_key(agent)
        if (
            isinstance(current_key, str)
            and current_key not in self._claimed_resumed_keys
            and self._run_state._current_agent is agent
            and self._entry_matches_agent_name(
                sessions_by_agent,
                current_key,
                base,
            )
        ):
            self._claimed_resumed_keys.add(current_key)
            self._resume_keys_by_agent_id[agent_id] = current_key
            return current_key

        available = self._resumed_keys_by_name().get(base, [])
        for key in available:
            if key in self._claimed_resumed_keys:
                continue
            if (
                isinstance(current_key, str)
                and key == current_key
                and self._run_state._current_agent is not agent
            ):
                continue
            self._claimed_resumed_keys.add(key)
            self._resume_keys_by_agent_id[agent_id] = key
            return key
        return None

    def _resumed_keys_by_name(self) -> dict[str, list[str]]:
        cached = self._available_resumed_keys_by_name
        if cached is not None:
            return cached

        grouped: dict[str, list[str]] = {}
        if self._run_state is not None and self._run_state._sandbox is not None:
            sessions_by_agent = self._run_state._sandbox.get("sessions_by_agent")
            if isinstance(sessions_by_agent, dict):
                for key, entry in sessions_by_agent.items():
                    if not isinstance(key, str):
                        continue
                    agent_name = self._agent_name_from_entry(key=key, entry=entry)
                    if agent_name is None:
                        continue
                    grouped.setdefault(agent_name, []).append(key)

        self._available_resumed_keys_by_name = grouped
        return grouped

    def _legacy_session_entries(self) -> dict[str, object]:
        if self._run_state is None or self._run_state._sandbox is None:
            return {}

        resumed = self._run_state._sandbox
        sessions_by_agent = resumed.get("sessions_by_agent")
        if isinstance(sessions_by_agent, dict):
            return {
                key: copy.deepcopy(entry)
                for key, entry in sessions_by_agent.items()
                if isinstance(key, str)
            }

        payload = resumed.get("session_state")
        if not isinstance(payload, dict):
            return {}

        current_key = resumed.get("current_agent_key")
        if isinstance(current_key, str):
            return {current_key: copy.deepcopy(payload)}

        current_agent_name = resumed.get("current_agent_name")
        if current_agent_name is None and self._run_state._current_agent is not None:
            current_agent_name = self._run_state._current_agent.name
        if isinstance(current_agent_name, str):
            return {current_agent_name: copy.deepcopy(payload)}

        current_agent_id = resumed.get("current_agent_id")
        if current_agent_id is not None:
            return {str(current_agent_id): copy.deepcopy(payload)}
        return {}

    def _serialize_sessions_by_agent(
        self,
        client: BaseSandboxClient[Any],
    ) -> dict[str, object]:
        sessions_by_agent = self._legacy_session_entries()
        for agent_id, agent_resources in self._resources_by_agent.items():
            agent = self._acquired_agents.get(agent_id)
            if agent is None:
                continue
            resume_key = self._ensure_resume_key(agent)
            source_key = self._resume_source_key_by_agent_id.get(agent_id)
            if source_key is not None and source_key != resume_key:
                sessions_by_agent.pop(source_key, None)
            sessions_by_agent[resume_key] = self._serialize_session_entry(
                agent=agent,
                session_state=client.serialize_session_state(agent_resources.state),
            )
        return sessions_by_agent

    def _used_resume_keys(self) -> set[str]:
        used = set(self._legacy_session_entries())
        used.update(self._resume_keys_by_agent_id.values())
        return used

    def _remember_resume_source_key(self, agent_id: int, key: str) -> None:
        self._resume_source_key_by_agent_id[agent_id] = key

    @staticmethod
    def _entry_matches_agent_name(
        sessions_by_agent: object,
        key: str,
        agent_name: str,
    ) -> bool:
        if not isinstance(sessions_by_agent, dict):
            return False
        entry = sessions_by_agent.get(key)
        return (
            SandboxRuntimeSessionManager._agent_name_from_entry(key=key, entry=entry) == agent_name
        )

    @staticmethod
    def _agent_name_from_entry(*, key: str, entry: object) -> str | None:
        if isinstance(entry, dict):
            entry_name = entry.get("agent_name")
            session_state = entry.get("session_state")
            if isinstance(entry_name, str) and isinstance(session_state, dict):
                return entry_name
            return key
        return None

    @staticmethod
    def _session_payload_from_entry(entry: object) -> dict[str, object] | None:
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError("RunState sandbox payload has an invalid `sessions_by_agent` item")
        session_state = entry.get("session_state")
        if isinstance(session_state, dict):
            return session_state
        return entry
