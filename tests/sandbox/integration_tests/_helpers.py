from __future__ import annotations

import io
import os
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from agents import function_tool
from agents.editor import ApplyPatchOperation
from agents.sandbox.capabilities import Capability
from agents.sandbox.entries import (
    AzureBlobMount,
    Dir,
    File,
    GCSMount,
    GitRepo,
    InContainerMountStrategy,
    LocalDir,
    LocalFile,
    R2Mount,
    RcloneMountPattern,
    S3Mount,
)
from agents.sandbox.errors import (
    ApplyPatchPathError,
    InvalidManifestPathError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.files import EntryKind
from agents.sandbox.manifest import Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.workspace_paths import SandboxPathGrant
from agents.tool import Tool

BUILTIN_MANIFEST_ENTRY_TYPES = {
    "azure_blob_mount",
    "dir",
    "file",
    "gcs_mount",
    "git_repo",
    "local_dir",
    "local_file",
    "r2_mount",
    "s3_mount",
}

DURABLE_WORKSPACE_TEXTS = {
    "inline.txt": "inline file v1\n",
    "delete_me.txt": "delete me v1\n",
    "tree/nested.txt": "nested file v1\n",
    "copied_file.txt": "local file source v1\n",
    "copied_dir/child.txt": "local dir child v1\n",
    "copied_dir/nested/grandchild.txt": "local dir grandchild v1\n",
    "repo/README.md": "mock git repo readme v1\n",
    "repo/pkg/module.py": "VALUE = 'mock git module v1'\n",
}

EPHEMERAL_WORKSPACE_TEXTS = {
    "tree/ephemeral.txt": "ephemeral file v1\n",
}

MOUNT_WORKSPACE_TEXTS = {
    "mounts/s3/.mock-rclone-mounted": "mock rclone mount\n",
    "mounts/gcs/.mock-rclone-mounted": "mock rclone mount\n",
    "mounts/r2/.mock-rclone-mounted": "mock rclone mount\n",
    "mounts/azure/.mock-rclone-mounted": "mock rclone mount\n",
}

ARCHIVE_WORKSPACE_TEXTS = {
    "archive_dir/hello.txt": "hello from tar archive\n",
}

RUNTIME_WORKSPACE_TEXTS = {
    "runtime_note.txt": "runtime note v1\n",
}

PATCHED_WORKSPACE_TEXTS = {
    "inline.txt": "inline file v2\n",
    "created_by_patch.txt": "created by patch",
}

RESTORED_WORKSPACE_DIRS = {
    "archive_dir",
    "copied_dir",
    "copied_dir/nested",
    "mounts",
    "mounts/azure",
    "mounts/gcs",
    "mounts/r2",
    "mounts/s3",
    "repo",
    "repo/pkg",
    "tree",
}

RESTORED_WORKSPACE_FILES = {
    "archive_dir/hello.txt",
    "bundle.tar",
    "copied_dir/child.txt",
    "copied_dir/nested/grandchild.txt",
    "copied_file.txt",
    "created_by_patch.txt",
    "inline.txt",
    "mounts/azure/.mock-rclone-mounted",
    "mounts/gcs/.mock-rclone-mounted",
    "mounts/r2/.mock-rclone-mounted",
    "mounts/s3/.mock-rclone-mounted",
    "repo/README.md",
    "repo/pkg/module.py",
    "runtime_note.txt",
    "tree/ephemeral.txt",
    "tree/nested.txt",
}

SANDBOX_INTERNAL_WORKSPACE_DIR_PREFIXES = (".sandbox-rclone-config",)

MOCK_TOOL_NAMES = (
    "blobfuse2",
    "cp",
    "fusermount3",
    "git",
    "mount-s3",
    "pkill",
    "rclone",
    "rm",
    "umount",
)


@dataclass(frozen=True)
class MockExternalTools:
    bin_dir: Path
    log_path: Path

    def calls(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").splitlines()


def install_mock_external_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> MockExternalTools:
    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    log_path = tmp_path / "mock-tool-calls.tsv"
    log_path.write_text("", encoding="utf-8")

    for name in MOCK_TOOL_NAMES:
        tool_path = bin_dir / name
        tool_path.write_text(_mock_tool_script(), encoding="utf-8")
        tool_path.chmod(0o755)

    existing_path = os.environ.get("PATH", "")
    monkeypatch.setenv("SANDBOX_INTEGRATION_TOOL_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{existing_path}")
    return MockExternalTools(bin_dir=bin_dir, log_path=log_path)


def create_local_sources(tmp_path: Path) -> Path:
    source_root = tmp_path / "manifest-sources"
    local_dir = source_root / "local-dir"
    nested_dir = local_dir / "nested"
    nested_dir.mkdir(parents=True)
    (source_root / "local-file.txt").write_text("local file source v1\n", encoding="utf-8")
    (local_dir / "child.txt").write_text("local dir child v1\n", encoding="utf-8")
    (nested_dir / "grandchild.txt").write_text("local dir grandchild v1\n", encoding="utf-8")
    return source_root


def build_manifest_with_all_entry_types(*, workspace_root: Path, source_root: Path) -> Manifest:
    return Manifest(
        root=str(workspace_root),
        extra_path_grants=(SandboxPathGrant(path=str(source_root)),),
        entries={
            "inline.txt": File(content=DURABLE_WORKSPACE_TEXTS["inline.txt"].encode("utf-8")),
            "delete_me.txt": File(content=DURABLE_WORKSPACE_TEXTS["delete_me.txt"].encode("utf-8")),
            "tree": Dir(
                children={
                    "nested.txt": File(
                        content=DURABLE_WORKSPACE_TEXTS["tree/nested.txt"].encode("utf-8")
                    ),
                    "ephemeral.txt": File(
                        content=EPHEMERAL_WORKSPACE_TEXTS["tree/ephemeral.txt"].encode("utf-8"),
                        ephemeral=True,
                    ),
                }
            ),
            "copied_file.txt": LocalFile(
                src=source_root / "local-file.txt",
            ),
            "copied_dir": LocalDir(
                src=source_root / "local-dir",
            ),
            "repo": GitRepo(repo="openai/mock-sandbox-fixture", ref="main"),
            "mounts/s3": S3Mount(
                bucket="s3-bucket",
                access_key_id="s3-access-key-id",
                secret_access_key="s3-secret-access-key",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            "mounts/gcs": GCSMount(
                bucket="gcs-bucket",
                access_id="gcs-access-id",
                secret_access_key="gcs-secret-access-key",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            "mounts/r2": R2Mount(
                bucket="r2-bucket",
                account_id="r2-account-id",
                access_key_id="r2-access-key-id",
                secret_access_key="r2-secret-access-key",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            "mounts/azure": AzureBlobMount(
                account="azure-account",
                container="azure-container",
                account_key="azure-account-key",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
        },
    )


def manifest_entry_types(manifest: Manifest) -> set[str]:
    return {entry.type for _path, entry in manifest.iter_entries()}


async def read_workspace_text(session: BaseSandboxSession, path: str | Path) -> str:
    handle = await session.read(Path(path))
    try:
        payload = handle.read()
    finally:
        handle.close()
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    raise TypeError(f"Unexpected workspace read payload type: {type(payload).__name__}")


async def write_workspace_text(session: BaseSandboxSession, path: str | Path, text: str) -> None:
    await session.write(Path(path), io.BytesIO(text.encode("utf-8")))


async def assert_workspace_texts(
    session: BaseSandboxSession,
    expected: Mapping[str, str],
) -> None:
    actual = {path: await read_workspace_text(session, path) for path in expected}
    assert actual == dict(expected)


async def assert_manifest_materialized(session: BaseSandboxSession) -> None:
    assert manifest_entry_types(session.state.manifest) == BUILTIN_MANIFEST_ENTRY_TYPES
    await assert_workspace_texts(session, DURABLE_WORKSPACE_TEXTS)
    await assert_workspace_texts(session, EPHEMERAL_WORKSPACE_TEXTS)
    await assert_workspace_texts(session, MOUNT_WORKSPACE_TEXTS)


async def assert_lifecycle_patch_state(session: BaseSandboxSession) -> None:
    await assert_workspace_texts(
        session,
        {
            **{
                path: text
                for path, text in DURABLE_WORKSPACE_TEXTS.items()
                if path != "delete_me.txt"
            },
            **RUNTIME_WORKSPACE_TEXTS,
            **PATCHED_WORKSPACE_TEXTS,
        },
    )
    await assert_workspace_missing(session, "delete_me.txt")


async def assert_restored_lifecycle_state(session: BaseSandboxSession) -> None:
    assert manifest_entry_types(session.state.manifest) == BUILTIN_MANIFEST_ENTRY_TYPES
    await assert_lifecycle_patch_state(session)
    await assert_workspace_texts(session, ARCHIVE_WORKSPACE_TEXTS)
    await assert_workspace_texts(session, EPHEMERAL_WORKSPACE_TEXTS)
    await assert_workspace_texts(session, MOUNT_WORKSPACE_TEXTS)
    await assert_restored_workspace_tree(session)


async def assert_workspace_missing(session: BaseSandboxSession, path: str) -> None:
    try:
        await read_workspace_text(session, path)
    except WorkspaceReadNotFoundError:
        return
    raise AssertionError(f"Expected workspace path to be missing: {path}")


async def assert_workspace_escape_blocked(session: BaseSandboxSession) -> None:
    for path in ("../outside.txt", "/tmp/sandbox-outside.txt"):
        await _assert_read_blocked(session, path)
        await _assert_write_blocked(session, path)
        await _assert_patch_blocked(session, path)
    await _assert_symlink_escape_blocked(session)


async def assert_restored_workspace_tree(session: BaseSandboxSession) -> None:
    actual_dirs, actual_files = await _workspace_tree(session)
    assert actual_dirs == RESTORED_WORKSPACE_DIRS, {
        "actual_dirs": sorted(actual_dirs),
        "expected_dirs": sorted(RESTORED_WORKSPACE_DIRS),
    }
    assert actual_files == RESTORED_WORKSPACE_FILES, {
        "actual_files": sorted(actual_files),
        "expected_files": sorted(RESTORED_WORKSPACE_FILES),
    }


def lifecycle_patch_operations() -> list[ApplyPatchOperation | dict[str, object]]:
    return [
        ApplyPatchOperation(
            type="update_file",
            path="inline.txt",
            diff="@@\n-inline file v1\n+inline file v2\n",
        ),
        ApplyPatchOperation(
            type="create_file",
            path="created_by_patch.txt",
            diff="+created by patch\n",
        ),
        ApplyPatchOperation(
            type="delete_file",
            path="delete_me.txt",
        ),
    ]


class SandboxFileCapability(Capability):
    type: str = "sandbox-file"

    def __init__(self) -> None:
        super().__init__(type="sandbox-file")

    def tools(self) -> list[Tool]:
        @function_tool(name_override="write_file", failure_error_function=None)
        async def write_file(path: str, content: str) -> str:
            if self.session is None:
                raise AssertionError("SandboxFileCapability is not bound to a session.")
            await write_workspace_text(self.session, path, content)
            return f"wrote {path}"

        @function_tool(name_override="read_file", failure_error_function=None)
        async def read_file(path: str) -> str:
            if self.session is None:
                raise AssertionError("SandboxFileCapability is not bound to a session.")
            return await read_workspace_text(self.session, path)

        return [write_file, read_file]


class SandboxLifecycleProbeCapability(Capability):
    type: str = "sandbox-lifecycle-probe"
    pty_process_id: int | None = None

    def __init__(self) -> None:
        super().__init__(type="sandbox-lifecycle-probe")

    def tools(self) -> list[Tool]:
        @function_tool(name_override="assert_manifest_materialized", failure_error_function=None)
        async def assert_manifest_materialized_tool() -> str:
            session = self._require_session()
            await assert_manifest_materialized(session)
            return "manifest materialized"

        @function_tool(name_override="apply_lifecycle_patch", failure_error_function=None)
        async def apply_lifecycle_patch() -> str:
            session = self._require_session()
            result = await session.apply_patch(lifecycle_patch_operations())
            assert result == "Done!"
            await assert_lifecycle_patch_state(session)
            return "lifecycle patch applied"

        @function_tool(name_override="assert_workspace_escape_blocked", failure_error_function=None)
        async def assert_workspace_escape_blocked_tool() -> str:
            session = self._require_session()
            await assert_workspace_escape_blocked(session)
            return "workspace escape blocked"

        @function_tool(name_override="extract_lifecycle_archive", failure_error_function=None)
        async def extract_lifecycle_archive() -> str:
            session = self._require_session()
            await session.extract("bundle.tar", _tar_bytes(ARCHIVE_WORKSPACE_TEXTS))
            await assert_workspace_texts(session, ARCHIVE_WORKSPACE_TEXTS)
            return "archive extracted"

        @function_tool(name_override="start_lifecycle_pty", failure_error_function=None)
        async def start_lifecycle_pty() -> str:
            session = self._require_session()
            pty = await session.pty_exec_start(
                "sh",
                "-c",
                "printf 'ready\\n'; while IFS= read -r line; do printf 'got:%s\\n' \"$line\"; done",
                shell=False,
                tty=True,
                yield_time_s=0.25,
            )
            assert pty.process_id is not None
            output = pty.output.decode("utf-8", errors="replace").replace("\r\n", "\n")
            assert output == "ready\n"
            self.pty_process_id = pty.process_id
            update = await session.pty_write_stdin(
                session_id=pty.process_id,
                chars="hello pty\n",
                yield_time_s=0.25,
            )
            write_output = update.output.decode("utf-8", errors="replace").replace("\r\n", "\n")
            assert write_output == "hello pty\ngot:hello pty\n"
            assert update.process_id == pty.process_id
            assert update.exit_code is None
            return "pty started and echoed stdin"

        @function_tool(name_override="assert_restored_lifecycle_state", failure_error_function=None)
        async def assert_restored_lifecycle_state_tool() -> str:
            session = self._require_session()
            await assert_restored_lifecycle_state(session)
            return "restored lifecycle state verified"

        return [
            assert_manifest_materialized_tool,
            apply_lifecycle_patch,
            assert_workspace_escape_blocked_tool,
            extract_lifecycle_archive,
            start_lifecycle_pty,
            assert_restored_lifecycle_state_tool,
        ]

    def _require_session(self) -> BaseSandboxSession:
        if self.session is None:
            raise AssertionError("SandboxLifecycleProbeCapability is not bound to a session.")
        return self.session


async def _assert_read_blocked(session: BaseSandboxSession, path: str) -> None:
    try:
        await read_workspace_text(session, path)
    except InvalidManifestPathError:
        return
    raise AssertionError(f"Expected workspace read to be blocked: {path}")


async def _assert_write_blocked(session: BaseSandboxSession, path: str) -> None:
    try:
        await write_workspace_text(session, path, "outside write\n")
    except InvalidManifestPathError:
        return
    raise AssertionError(f"Expected workspace write to be blocked: {path}")


async def _assert_patch_blocked(session: BaseSandboxSession, path: str) -> None:
    try:
        await session.apply_patch(
            ApplyPatchOperation(
                type="create_file",
                path=path,
                diff="+outside patch\n",
            )
        )
    except (ApplyPatchPathError, InvalidManifestPathError):
        return
    raise AssertionError(f"Expected workspace patch to be blocked: {path}")


async def _assert_symlink_escape_blocked(session: BaseSandboxSession) -> None:
    workspace_root = Path(session.state.manifest.root)
    outside_path = workspace_root.parent / "symlink-outside.txt"
    symlink_path = workspace_root / "symlink_escape.txt"
    outside_path.write_text("outside symlink target\n", encoding="utf-8")
    symlink_path.symlink_to(outside_path)
    try:
        await _assert_read_blocked(session, "symlink_escape.txt")
        await _assert_write_blocked(session, "symlink_escape.txt")
        await _assert_patch_blocked(session, "symlink_escape.txt")
    finally:
        symlink_path.unlink(missing_ok=True)
        outside_path.unlink(missing_ok=True)


def _tar_bytes(members: Mapping[str, str]) -> io.BytesIO:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, text in members.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    archive.seek(0)
    return archive


async def _workspace_tree(session: BaseSandboxSession) -> tuple[set[str], set[str]]:
    root = Path(session.state.manifest.root).resolve(strict=False)
    dirs: set[str] = set()
    files: set[str] = set()

    async def collect(path: Path) -> None:
        for entry in await session.ls(path):
            rel_path = _entry_workspace_rel_path(entry.path, root)
            if entry.kind == EntryKind.DIRECTORY:
                if _is_sandbox_internal_workspace_dir(rel_path):
                    continue
                dirs.add(rel_path)
                await collect(Path(rel_path))
            elif entry.kind == EntryKind.FILE:
                files.add(rel_path)
            else:
                raise AssertionError(
                    f"Unexpected workspace entry kind for {rel_path}: {entry.kind}"
                )

    await collect(Path("."))
    return dirs, files


def _entry_workspace_rel_path(entry_path: str, root: Path) -> str:
    path = Path(entry_path)
    if path.is_absolute():
        path = path.resolve(strict=False).relative_to(root)
    return path.as_posix()


def _is_sandbox_internal_workspace_dir(path: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in SANDBOX_INTERNAL_WORKSPACE_DIR_PREFIXES
    )


def _mock_tool_script() -> str:
    return """#!/bin/sh
set -eu

tool=$(basename "$0")
log_path="${SANDBOX_INTEGRATION_TOOL_LOG:-}"
if [ -n "$log_path" ]; then
  {
    printf "%s" "$tool"
    for arg in "$@"; do
      printf "\\t%s" "$arg"
    done
    printf "\\n"
  } >> "$log_path"
fi

case "$tool" in
  git)
    exit 0
    ;;
  cp)
    dest=""
    for arg in "$@"; do
      dest="$arg"
    done
    mkdir -p "$dest/pkg"
    printf "mock git repo readme v1\\n" > "$dest/README.md"
    printf "VALUE = 'mock git module v1'\\n" > "$dest/pkg/module.py"
    exit 0
    ;;
  rclone)
    if [ "${1:-}" = "mount" ] && [ -n "${3:-}" ]; then
      mkdir -p "$3"
      printf "mock rclone mount\\n" > "$3/.mock-rclone-mounted"
    fi
    exit 0
    ;;
  blobfuse2)
    if [ "${1:-}" = "mount" ]; then
      dest=""
      for arg in "$@"; do
        dest="$arg"
      done
      mkdir -p "$dest"
      printf "mock blobfuse mount\\n" > "$dest/.mock-blobfuse-mounted"
    fi
    exit 0
    ;;
  mount-s3)
    dest=""
    for arg in "$@"; do
      dest="$arg"
    done
    mkdir -p "$dest"
    printf "mock mount-s3 mount\\n" > "$dest/.mock-mount-s3-mounted"
    exit 0
    ;;
  rm)
    recursive=""
    for arg in "$@"; do
      case "$arg" in
        -rf|-fr|-r|-f|--)
          if [ "$arg" = "-rf" ] || [ "$arg" = "-fr" ] || [ "$arg" = "-r" ]; then
            recursive="-r"
          fi
          ;;
        "$HOME"|"$HOME"/*)
          if [ -n "$recursive" ]; then
            /bin/rm -rf -- "$arg"
          else
            /bin/rm -f -- "$arg"
          fi
          ;;
        /*)
          ;;
        *..*)
          ;;
        *)
          if [ -n "$recursive" ]; then
            /bin/rm -rf -- "$arg"
          else
            /bin/rm -f -- "$arg"
          fi
          ;;
      esac
    done
    exit 0
    ;;
  fusermount3|umount|pkill)
    exit 0
    ;;
esac

exit 0
"""
