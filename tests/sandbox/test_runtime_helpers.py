from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from agents.sandbox.session.runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    RuntimeHelperScript,
)

requires_posix_shell = pytest.mark.skipif(
    sys.platform == "win32",
    reason="runtime helper shell script tests require a POSIX shell",
)


def _install_resolve_helper(tmp_path: Path) -> Path:
    helper_path = tmp_path / "resolve-workspace-path"
    helper_path.write_text(RESOLVE_WORKSPACE_PATH_HELPER.content, encoding="utf-8")
    helper_path.chmod(0o755)
    return helper_path


def test_runtime_helper_from_content_uses_posix_install_path() -> None:
    helper = RuntimeHelperScript.from_content(
        name="test-helper",
        content="#!/bin/sh\nprintf 'ok\\n'",
    )

    assert isinstance(helper.install_path, PurePosixPath)
    assert helper.install_path.as_posix().startswith("/tmp/openai-agents/bin/test-helper-")
    assert str(helper.install_path).startswith("/tmp/openai-agents/bin/test-helper-")


@requires_posix_shell
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
            "0",
            str(extra_root),
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == f"{target.resolve(strict=False)}\n"
    assert result.stderr == ""


@requires_posix_shell
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
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 111
    assert result.stdout == ""
    assert result.stderr == f"workspace escape: {target.resolve(strict=False)}\n"


@requires_posix_shell
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
            "0",
            str(root_alias),
            "0",
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


@requires_posix_shell
def test_resolve_workspace_path_helper_rejects_nested_read_only_extra_grant_on_write(
    tmp_path: Path,
) -> None:
    helper_path = _install_resolve_helper(tmp_path)
    workspace = tmp_path / "workspace"
    extra_root = tmp_path / "tmp"
    protected_root = extra_root / "protected"
    workspace.mkdir()
    protected_root.mkdir(parents=True)
    target = protected_root / "result.txt"
    target.write_text("scratch output", encoding="utf-8")
    (workspace / "tmp-link").symlink_to(extra_root, target_is_directory=True)

    result = subprocess.run(
        [
            str(helper_path),
            str(workspace),
            str(workspace / "tmp-link" / "protected" / "result.txt"),
            "1",
            str(extra_root),
            "0",
            str(protected_root),
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 114
    assert result.stdout == ""
    assert result.stderr == (
        f"read-only extra path grant: {protected_root}\n"
        f"resolved path: {target.resolve(strict=False)}\n"
    )


@requires_posix_shell
def test_resolve_workspace_path_helper_allows_nested_read_only_extra_grant_on_read(
    tmp_path: Path,
) -> None:
    helper_path = _install_resolve_helper(tmp_path)
    workspace = tmp_path / "workspace"
    extra_root = tmp_path / "tmp"
    protected_root = extra_root / "protected"
    workspace.mkdir()
    protected_root.mkdir(parents=True)
    target = protected_root / "result.txt"
    target.write_text("scratch output", encoding="utf-8")
    (workspace / "tmp-link").symlink_to(extra_root, target_is_directory=True)

    result = subprocess.run(
        [
            str(helper_path),
            str(workspace),
            str(workspace / "tmp-link" / "protected" / "result.txt"),
            "0",
            str(extra_root),
            "0",
            str(protected_root),
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == f"{target.resolve(strict=False)}\n"
    assert result.stderr == ""
