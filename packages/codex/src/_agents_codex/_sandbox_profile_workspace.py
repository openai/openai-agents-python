"""Prepare one additive file for a captured profile on its disposable workspace."""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _agents_shared.blocking_workspace_io import run_blocking_workspace_io
from agents.classic.sandbox.entries import Dir, File
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.session.sandbox_session import SandboxSession

ATTRIBUTE = "_sdk_owned_profile_workspace"


@dataclass
class _Scope:
    capabilities: tuple[Any, ...]
    additions: tuple[tuple[Path, File], ...]
    contributions: Any = None


class _OwnedProfileWorkspace:
    def __init__(self, capabilities: tuple[Any, ...], manifest: Manifest, eligible: bool):
        self._capabilities = capabilities
        self._manifest = manifest
        self._eligible = eligible
        self._scopes: dict[str, _Scope] = {}
        self._resource: SandboxSession | None = None
        self._state: Any = None
        self._environment: Any = None
        self._failed = False
        self._retired = False

    @property
    def changed(self) -> bool:
        return any(scope.additions for scope in self._scopes.values())

    def capture(self, ids: tuple[str, ...]) -> None:
        from .sandbox import _capture_capabilities, _manifest

        if self._resource is not None:
            raise ValueError("Sandbox profiles require their fresh unbound source")
        if tuple(self._scopes) == ids:
            return
        scopes: dict[str, _Scope] = {}
        for index, profile in enumerate(ids):
            capabilities = (
                self._capabilities if index == 0 else _capture_capabilities(self._capabilities)
            )
            manifest = self._manifest.model_copy(deep=True)
            if index:
                for capability in capabilities:
                    manifest = _manifest(capability.prepare_manifest(manifest))
            additions = self._additions(manifest)
            if additions and not self._eligible:
                raise ValueError("Captured manifest changes require an owned disposable workspace")
            for path, entry in additions:
                if any(
                    path == previous_path and entry != previous_entry
                    for previous in scopes.values()
                    for previous_path, previous_entry in previous.additions
                ):
                    raise ValueError("Captured workspace file declarations conflict")
            scopes[profile] = _Scope(capabilities, additions)
        self._scopes = scopes

    def _additions(self, target: Manifest) -> tuple[tuple[Path, File], ...]:
        if self._manifest.model_copy(update={"entries": {}}) != target.model_copy(
            update={"entries": {}}
        ):
            raise ValueError("Captured workspace authority must remain unchanged")
        before, after = dict(self._manifest.iter_entries()), dict(target.iter_entries())
        if before.keys() - after.keys():
            raise ValueError("Captured workspace entries cannot be removed")
        for path in before.keys() & after.keys():
            left, right = before[path], after[path]
            if isinstance(left, Dir) and isinstance(right, Dir):
                left = left.model_copy(update={"children": {}})
                right = right.model_copy(update={"children": {}})
            if left != right:
                raise ValueError("Captured workspace entries cannot be replaced")
        added = after.keys() - before.keys()
        if not added:
            return ()
        if len(added) == 1:
            name = next(iter(added))
            path = Path(name)
            entry = after[name]
            if (
                type(entry) is File
                and entry.group is None
                and not entry.ephemeral
                and entry.permissions == File(content=b"").permissions
                and len(path.parts) == 1
            ):
                return ((path, entry),)
        raise ValueError("Captured workspace supports one new plain root file per profile")

    def reserve(self, resource: SandboxSession) -> None:
        if self.changed:
            self._resource = resource
            self._state = resource.state
            self._expected_manifest = resource.state.manifest.model_copy(deep=True)
            self._resource_key = (
                resource.state.type,
                resource.state.session_id,
                resource.state.workspace_root_ready,
                getattr(resource.state, "workspace_root_owned", None),
            )

    async def attach(
        self, resource: SandboxSession, environment: Any, user: Any, root_contributions: Any
    ) -> None:
        from ._function_tools import FunctionTool
        from .sandbox import _capability_workspace, _SandboxContributions

        self._environment = environment
        if not self.changed:
            return
        self._check(lambda: True)
        if resource is not self._resource:
            raise RuntimeError("Captured workspace resource changed")
        inner = resource._inner
        while isinstance(inner, SandboxSession):
            inner = inner._inner
        from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxSession

        if (
            not self._eligible
            or type(inner) is not UnixLocalSandboxSession
            or not inner.state.workspace_root_owned
            or user is not None
        ):
            raise ValueError("Captured manifest changes require a fresh owned UnixLocal workspace")
        self._inner = inner
        for index, scope in enumerate(self._scopes.values()):
            if not index:
                scope.contributions = root_contributions
                continue
            workspace = _capability_workspace(resource, environment)
            for capability in scope.capabilities:
                await capability.bind(resource, user=user, workspace=workspace)
                self._check(lambda: True)
            tools = tuple(tool for capability in scope.capabilities for tool in capability.tools())
            if any(
                not isinstance(tool, FunctionTool)
                or tool._bind is None
                or tool.needs_approval is not False
                or tool.defer_loading
                for tool in tools
            ):
                raise TypeError("Sandbox capability tools must be eager automatic callbacks")
            scope.contributions = _SandboxContributions(tools, scope.capabilities)

    def contributions(self, profile: str) -> Any:
        return self._scopes[profile].contributions

    def _check(self, active: Callable[[], bool]) -> SandboxSession:
        if self._failed or self._retired or not active() or self._resource is None:
            raise RuntimeError("Captured workspace owner is retired")
        state = self._resource.state
        key = (
            state.type,
            state.session_id,
            state.workspace_root_ready,
            getattr(state, "workspace_root_owned", None),
        )
        if (
            state is not self._state
            or key != self._resource_key
            or state.manifest != self._expected_manifest
        ):
            raise RuntimeError("Captured workspace resource state changed")
        return self._resource

    async def prepare(self, profile: str, active: Callable[[], bool]) -> str:
        from .sandbox import _render_workspace_instructions
        from .sandbox_skills import _append_skill_instructions

        resource = self._check(active)
        scope = self._scopes[profile]
        try:
            current = dict(resource.state.manifest.iter_entries())
            for path, entry in scope.additions:
                if path not in current:
                    self._check(active)
                    # This private hook claims the leaf with O_EXCL and O_NOFOLLOW.
                    # Source closure joins this entered operation before disposal.
                    await run_blocking_workspace_io(self._write_file, path, entry)
                    self._check(active)
                    updated = resource.state.manifest.model_copy(deep=True)
                    updated.entries[path] = entry.model_copy(deep=True)
                    resource.state = resource.state.model_copy(update={"manifest": updated})
                    self._state = resource.state
                    self._expected_manifest = updated.model_copy(deep=True)
            text = _render_workspace_instructions(self._state.manifest, self._environment)
            for capability in scope.capabilities:
                text = _append_skill_instructions(
                    text, await capability.instructions(self._state.manifest)
                )
                self._check(active)
            return text
        except BaseException:
            self._failed = True
            raise

    def _write_file(self, path: Path, entry: File) -> None:
        # The qualified provider normalizes the parent and authorizes this write.
        # The file owner sets metadata on the claimed descriptor, never a raced name.
        target = self._inner.normalize_path(path.parent, for_write=True) / path.name
        self._inner._files.write_new(
            target, io.BytesIO(entry.content), mode=entry.permissions.to_mode()
        )

    def retire(self) -> None:
        self._retired = True
