from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.errors import InvalidManifestPathError, WorkspaceArchiveWriteError
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


def test_normalize_path_with_symlink_resolution(tmp_path: Path) -> None:
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
            method=lambda policy, path: policy.normalize_path(path, resolve_symlinks=True),
            test_case=test_case,
            root=alias,
        )


def test_manifest_serializes_extra_path_grants() -> None:
    manifest = Manifest(
        extra_path_grants=(
            SandboxPathGrant(
                path="/tmp",
                description="temporary files",
            ),
            SandboxPathGrant(
                path="/opt/toolchain",
                read_only=True,
                description="compiler runtime",
            ),
        ),
    )

    assert manifest.model_dump(mode="json")["extra_path_grants"] == [
        {
            "path": "/tmp",
            "read_only": False,
            "description": "temporary files",
        },
        {
            "path": "/opt/toolchain",
            "read_only": True,
            "description": "compiler runtime",
        },
    ]


def test_extra_path_grant_accepts_absolute_path() -> None:
    policy = WorkspacePathPolicy(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/tmp"),),
    )

    assert policy.normalize_path("/tmp/result.txt") == Path("/tmp/result.txt")


def test_extra_path_grant_rejects_ungranted_absolute_path() -> None:
    policy = WorkspacePathPolicy(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/tmp"),),
    )

    with pytest.raises(InvalidManifestPathError) as exc_info:
        policy.normalize_path("/var/result.txt")

    assert str(exc_info.value) == "manifest path must be relative: /var/result.txt"
    assert exc_info.value.context == {"rel": "/var/result.txt", "reason": "absolute"}


def test_extra_path_grant_rejects_write_under_read_only_grant() -> None:
    policy = WorkspacePathPolicy(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/opt/toolchain", read_only=True),),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        policy.normalize_path("/opt/toolchain/cache.db", for_write=True)

    assert str(exc_info.value) == "failed to write archive for path: /opt/toolchain/cache.db"
    assert exc_info.value.context == {
        "path": "/opt/toolchain/cache.db",
        "reason": "read_only_extra_path_grant",
        "grant_path": "/opt/toolchain",
    }


def test_extra_path_grant_allows_read_under_read_only_grant() -> None:
    policy = WorkspacePathPolicy(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/opt/toolchain", read_only=True),),
    )

    assert policy.normalize_path("/opt/toolchain/cache.db") == Path("/opt/toolchain/cache.db")


def test_host_io_rejects_write_under_resolved_read_only_extra_path_grant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    grant_alias = tmp_path / "allowed-alias"
    workspace.mkdir()
    allowed.mkdir()
    os.symlink(allowed, grant_alias, target_is_directory=True)
    target = allowed / "cache.db"
    policy = WorkspacePathPolicy(
        root=workspace,
        extra_path_grants=(SandboxPathGrant(path=str(grant_alias), read_only=True),),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        policy.normalize_path(target, for_write=True, resolve_symlinks=True)

    assert str(exc_info.value) == f"failed to write archive for path: {target}"
    assert exc_info.value.context == {
        "path": str(target),
        "reason": "read_only_extra_path_grant",
        "grant_path": str(grant_alias),
    }


def test_extra_path_grant_rejects_relative_path() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SandboxPathGrant(path="tmp")

    errors = exc_info.value.errors(include_url=False)
    assert len(errors) == 1
    error = dict(errors[0])
    ctx = cast(dict[str, Any], error["ctx"])
    error["ctx"] = {"error": str(ctx["error"])}
    assert error == {
        "type": "value_error",
        "loc": ("path",),
        "msg": "Value error, sandbox path grant path must be absolute",
        "input": "tmp",
        "ctx": {"error": "sandbox path grant path must be absolute"},
    }


def test_extra_path_grant_rejects_root_path() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SandboxPathGrant(path="/")

    errors = exc_info.value.errors(include_url=False)
    assert len(errors) == 1
    error = dict(errors[0])
    ctx = cast(dict[str, Any], error["ctx"])
    error["ctx"] = {"error": str(ctx["error"])}
    assert error == {
        "type": "value_error",
        "loc": ("path",),
        "msg": "Value error, sandbox path grant path must not be filesystem root",
        "input": "/",
        "ctx": {"error": "sandbox path grant path must not be filesystem root"},
    }


def test_extra_path_grant_rejects_root_alias_path() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SandboxPathGrant(path="//")

    errors = exc_info.value.errors(include_url=False)
    assert len(errors) == 1
    error = dict(errors[0])
    ctx = cast(dict[str, Any], error["ctx"])
    error["ctx"] = {"error": str(ctx["error"])}
    assert error == {
        "type": "value_error",
        "loc": ("path",),
        "msg": "Value error, sandbox path grant path must not be filesystem root",
        "input": "//",
        "ctx": {"error": "sandbox path grant path must not be filesystem root"},
    }


def test_host_io_rejects_extra_path_grant_symlink_to_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root_alias = tmp_path / "root-alias"
    workspace.mkdir()
    os.symlink(Path("/"), root_alias, target_is_directory=True)
    policy = WorkspacePathPolicy(
        root=workspace,
        extra_path_grants=(SandboxPathGrant(path=str(root_alias)),),
    )

    with pytest.raises(ValueError) as exc_info:
        policy.normalize_path(Path("/etc/passwd"), resolve_symlinks=True)

    assert str(exc_info.value) == "sandbox path grant path must not resolve to filesystem root"
