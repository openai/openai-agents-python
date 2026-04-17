from __future__ import annotations

import subprocess
from pathlib import Path

from agents.sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER


def _install_resolve_helper(tmp_path: Path) -> Path:
    helper_path = tmp_path / "resolve-workspace-path"
    helper_path.write_text(RESOLVE_WORKSPACE_PATH_HELPER.content, encoding="utf-8")
    helper_path.chmod(0o755)
    return helper_path


def test_resolve_workspace_path_helper_allows_extra_root_symlink_target(tmp_path: Path) -> None:
    helper_path = _install_resolve_helper(tmp_path)
    workspace = tmp_path / "workspace"
    extra_root = tmp_path / "tmp"
    workspace.mkdir()
    extra_root.mkdir()
    target = extra_root / "result.txt"
    target.write_text("scratch output", encoding="utf-8")
    (workspace / "tmp-link").symlink_to(extra_root, target_is_directory=True)

    result = subprocess.run(
        [
            str(helper_path),
            str(workspace),
            str(workspace / "tmp-link" / "result.txt"),
            str(extra_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == f"{target.resolve(strict=False)}\n"
    assert result.stderr == ""


def test_resolve_workspace_path_helper_rejects_extra_root_when_not_allowed(
    tmp_path: Path,
) -> None:
    helper_path = _install_resolve_helper(tmp_path)
    workspace = tmp_path / "workspace"
    extra_root = tmp_path / "tmp"
    workspace.mkdir()
    extra_root.mkdir()
    target = extra_root / "result.txt"
    target.write_text("scratch output", encoding="utf-8")
    (workspace / "tmp-link").symlink_to(extra_root, target_is_directory=True)

    result = subprocess.run(
        [
            str(helper_path),
            str(workspace),
            str(workspace / "tmp-link" / "result.txt"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 111
    assert result.stdout == ""
    assert result.stderr == f"workspace escape: {target.resolve(strict=False)}\n"


def test_resolve_workspace_path_helper_rejects_extra_root_symlink_to_root(
    tmp_path: Path,
) -> None:
    helper_path = _install_resolve_helper(tmp_path)
    workspace = tmp_path / "workspace"
    root_alias = tmp_path / "root-alias"
    workspace.mkdir()
    root_alias.symlink_to(Path("/"), target_is_directory=True)

    result = subprocess.run(
        [
            str(helper_path),
            str(workspace),
            "/etc/passwd",
            str(root_alias),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 113
    assert result.stdout == ""
    assert result.stderr == (
        f"extra path grant must not resolve to filesystem root: {root_alias}\n"
    )
