from __future__ import annotations

import io
from pathlib import Path

from agents import ApplyPatchTool, apply_diff
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.sandbox import Capability, Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.tool import Tool


def _read_text(handle: io.IOBase) -> str:
    payload = handle.read()
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    return str(payload)


class _SandboxWorkspaceEditor:
    def __init__(self, session: BaseSandboxSession) -> None:
        self._session = session

    async def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        target = self._resolve_path(operation.path)
        content = apply_diff("", operation.diff or "", mode="create")
        await self._session.mkdir(target.parent, parents=True)
        await self._session.write(target, io.BytesIO(content.encode("utf-8")))
        return ApplyPatchResult(output=f"Created {self._display_path(target)}")

    async def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        target = self._resolve_path(operation.path)
        handle = await self._session.read(target)
        try:
            original = _read_text(handle)
        finally:
            handle.close()
        updated = apply_diff(original, operation.diff or "")
        await self._session.write(target, io.BytesIO(updated.encode("utf-8")))
        return ApplyPatchResult(output=f"Updated {self._display_path(target)}")

    async def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        target = self._resolve_path(operation.path)
        await self._session.rm(target)
        return ApplyPatchResult(output=f"Deleted {self._display_path(target)}")

    def _resolve_path(self, raw_path: str) -> Path:
        return self._session.normalize_path(raw_path)

    def _display_path(self, path: Path) -> str:
        root = Path(self._session.state.manifest.root)
        return path.relative_to(root).as_posix()


class WorkspaceApplyPatchCapability(Capability):
    """Expose the hosted apply_patch tool against the active sandbox workspace."""

    def __init__(self) -> None:
        super().__init__(type="workspace_apply_patch")
        self._session: BaseSandboxSession | None = None

    def bind(self, session: BaseSandboxSession) -> None:
        self._session = session

    def tools(self) -> list[Tool]:
        if self._session is None:
            return []
        return [ApplyPatchTool(editor=_SandboxWorkspaceEditor(self._session))]

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        return (
            "Use the `apply_patch` tool for workspace text edits when you need to create or "
            "update files inside the sandbox. Prefer saving final outputs in the requested "
            "workspace directories instead of describing edits without writing them."
        )
