from __future__ import annotations

from pathlib import Path, PureWindowsPath

from agents.sandbox.workspace_paths import WorkspacePathPolicy


def test_resolved_path_normalizes_pure_windows_input(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("hello", encoding="utf-8")

    policy = WorkspacePathPolicy(root=workspace)

    assert policy.normalize_path(
        PureWindowsPath(r"nested\..\target.txt"),
        resolve_symlinks=True,
    ) == target.resolve()
