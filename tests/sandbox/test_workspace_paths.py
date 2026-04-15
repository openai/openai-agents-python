from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agents.sandbox.errors import InvalidManifestPathError
from agents.sandbox.workspace_paths import WorkspacePathPolicy

PathInput = str | Path
PathPolicyMethod = Callable[[WorkspacePathPolicy, PathInput], Path]


@dataclass(frozen=True)
class WorkspacePathCase:
    name: str
    path: PathInput
    expected: Path | None = None
    error_message: str | None = None
    error_context: dict[str, str] | None = None


def _policy(root: Path | str = "/workspace") -> WorkspacePathPolicy:
    return WorkspacePathPolicy(root=root)


def _assert_workspace_path_case(
    *,
    method: PathPolicyMethod,
    test_case: WorkspacePathCase,
    root: Path | str = "/workspace",
) -> None:
    if test_case.error_message is None:
        assert method(_policy(root), test_case.path) == test_case.expected
        return

    with pytest.raises(InvalidManifestPathError) as exc_info:
        method(_policy(root), test_case.path)

    assert str(exc_info.value) == test_case.error_message
    assert exc_info.value.context == test_case.error_context


ABSOLUTE_WORKSPACE_PATH_CASES = [
    WorkspacePathCase(
        name="relative path anchors under root",
        path="pkg/file.py",
        expected=Path("/workspace/pkg/file.py"),
    ),
    WorkspacePathCase(
        name="Path input anchors under root",
        path=Path("pkg/file.py"),
        expected=Path("/workspace/pkg/file.py"),
    ),
    WorkspacePathCase(
        name="absolute path inside root is accepted",
        path="/workspace/pkg/file.py",
        expected=Path("/workspace/pkg/file.py"),
    ),
    WorkspacePathCase(
        name="absolute path inside root is normalized",
        path="/workspace/pkg/../file.py",
        expected=Path("/workspace/file.py"),
    ),
    WorkspacePathCase(
        name="relative parent segment inside root is normalized",
        path="pkg/../secret.txt",
        expected=Path("/workspace/secret.txt"),
    ),
    WorkspacePathCase(
        name="absolute path outside root is rejected",
        path="/tmp/secret.txt",
        error_message="manifest path must be relative: /tmp/secret.txt",
        error_context={"rel": "/tmp/secret.txt", "reason": "absolute"},
    ),
    WorkspacePathCase(
        name="relative parent traversal is rejected",
        path="../secret.txt",
        error_message="manifest path must not escape root: ../secret.txt",
        error_context={"rel": "../secret.txt", "reason": "escape_root"},
    ),
    WorkspacePathCase(
        name="nested relative parent traversal outside root is rejected",
        path="pkg/../../secret.txt",
        error_message="manifest path must not escape root: pkg/../../secret.txt",
        error_context={"rel": "pkg/../../secret.txt", "reason": "escape_root"},
    ),
]


@pytest.mark.parametrize(
    "test_case",
    ABSOLUTE_WORKSPACE_PATH_CASES,
    ids=lambda test_case: test_case.name,
)
def test_absolute_workspace_path(test_case: WorkspacePathCase) -> None:
    _assert_workspace_path_case(
        method=lambda policy, path: policy.absolute_workspace_path(path),
        test_case=test_case,
    )


RELATIVE_PATH_CASES = [
    WorkspacePathCase(
        name="relative path stays relative",
        path="pkg/file.py",
        expected=Path("pkg/file.py"),
    ),
    WorkspacePathCase(
        name="absolute path inside root becomes relative",
        path="/workspace/pkg/file.py",
        expected=Path("pkg/file.py"),
    ),
    WorkspacePathCase(
        name="relative parent segment inside root is normalized",
        path="pkg/../secret.txt",
        expected=Path("secret.txt"),
    ),
    WorkspacePathCase(
        name="workspace root becomes dot",
        path="/workspace",
        expected=Path("."),
    ),
    WorkspacePathCase(
        name="provider root is not exposed",
        path="/provider/private/root/images/dot.png",
        expected=Path("images/dot.png"),
    ),
    WorkspacePathCase(
        name="relative provider path stays relative",
        path="images/dot.png",
        expected=Path("images/dot.png"),
    ),
    WorkspacePathCase(
        name="absolute path outside root is rejected",
        path="/tmp/secret.txt",
        error_message="manifest path must be relative: /tmp/secret.txt",
        error_context={"rel": "/tmp/secret.txt", "reason": "absolute"},
    ),
    WorkspacePathCase(
        name="relative parent traversal is rejected",
        path="../secret.txt",
        error_message="manifest path must not escape root: ../secret.txt",
        error_context={"rel": "../secret.txt", "reason": "escape_root"},
    ),
]


@pytest.mark.parametrize(
    "test_case",
    RELATIVE_PATH_CASES,
    ids=lambda test_case: test_case.name,
)
def test_relative_path(test_case: WorkspacePathCase) -> None:
    root = "/provider/private/root" if "provider" in test_case.name else "/workspace"
    _assert_workspace_path_case(
        method=lambda policy, path: policy.relative_path(path),
        test_case=test_case,
        root=root,
    )


def test_normalize_path_for_host_io(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    target = workspace / "target.txt"
    target.write_text("hello", encoding="utf-8")
    os.symlink(target, workspace / "link.txt")
    os.symlink(outside, workspace / "outside-link", target_is_directory=True)

    alias = tmp_path / "workspace-alias"
    os.symlink(workspace, alias, target_is_directory=True)

    test_cases = [
        WorkspacePathCase(
            name="relative path resolves under host root",
            path="target.txt",
            expected=target.resolve(),
        ),
        WorkspacePathCase(
            name="relative parent segment inside root resolves under host root",
            path="nested/../target.txt",
            expected=target.resolve(),
        ),
        WorkspacePathCase(
            name="safe internal leaf symlink resolves to target",
            path="link.txt",
            expected=target.resolve(),
        ),
        WorkspacePathCase(
            name="absolute path through root alias is accepted",
            path=alias / "target.txt",
            expected=target.resolve(),
        ),
        WorkspacePathCase(
            name="absolute resolved root path is accepted",
            path=target,
            expected=target.resolve(),
        ),
        WorkspacePathCase(
            name="symlink parent escape is rejected",
            path="outside-link/secret.txt",
            error_message="manifest path must not escape root: outside-link/secret.txt",
            error_context={"rel": "outside-link/secret.txt", "reason": "escape_root"},
        ),
        WorkspacePathCase(
            name="absolute path outside root is rejected",
            path=outside / "secret.txt",
            error_message=f"manifest path must be relative: {outside / 'secret.txt'}",
            error_context={"rel": str(outside / "secret.txt"), "reason": "absolute"},
        ),
    ]

    for test_case in test_cases:
        _assert_workspace_path_case(
            method=lambda policy, path: policy.normalize_path_for_host_io(path),
            test_case=test_case,
            root=alias,
        )
