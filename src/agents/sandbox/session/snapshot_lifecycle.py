from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import ExecNonZeroError
from ..files import EntryKind
from ..snapshot import NoopSnapshot
from ..workspace_paths import coerce_posix_path, posix_path_as_path
from .runtime_helpers import WORKSPACE_FINGERPRINT_HELPER

if TYPE_CHECKING:
    from .base_sandbox_session import BaseSandboxSession

SNAPSHOT_FINGERPRINT_VERSION = "workspace_tar_sha256_v1"


async def persist_snapshot(session: BaseSandboxSession) -> None:
    if isinstance(session.state.snapshot, NoopSnapshot):
        return

    fingerprint_record: dict[str, str] | None = None
    try:
        fingerprint_record = await session._compute_and_cache_snapshot_fingerprint()
    except Exception:
        fingerprint_record = None

    workspace_archive = await session.persist_workspace()
    try:
        await session.state.snapshot.persist(workspace_archive, dependencies=session.dependencies)
    except Exception:
        if fingerprint_record is not None:
            await session._delete_cached_snapshot_fingerprint_best_effort()
        raise
    finally:
        _close_best_effort(workspace_archive)

    if fingerprint_record is None:
        session.state.snapshot_fingerprint = None
        session.state.snapshot_fingerprint_version = None
        return

    session.state.snapshot_fingerprint = fingerprint_record["fingerprint"]
    session.state.snapshot_fingerprint_version = fingerprint_record["version"]


async def restore_snapshot_into_workspace_on_resume(session: BaseSandboxSession) -> None:
    await session._clear_workspace_root_on_resume()
    workspace_archive = await session.state.snapshot.restore(dependencies=session.dependencies)
    try:
        await session.hydrate_workspace(workspace_archive)
    finally:
        _close_best_effort(workspace_archive)


async def live_workspace_matches_snapshot_on_resume(session: BaseSandboxSession) -> bool:
    stored_fingerprint = session.state.snapshot_fingerprint
    stored_version = session.state.snapshot_fingerprint_version
    if not stored_fingerprint or not stored_version:
        return False

    try:
        cached_record = await session._compute_and_cache_snapshot_fingerprint()
    except Exception:
        return False

    return (
        cached_record.get("fingerprint") == stored_fingerprint
        and cached_record.get("version") == stored_version
    )


async def can_skip_snapshot_restore_on_resume(
    session: BaseSandboxSession,
    *,
    is_running: bool,
) -> bool:
    if not is_running:
        return False
    return await live_workspace_matches_snapshot_on_resume(session)


def snapshot_fingerprint_cache_path(session: BaseSandboxSession) -> Path:
    cache_path = coerce_posix_path(
        f"/tmp/openai-agents/session-state/{session.state.session_id.hex}/fingerprint.json"
    )
    if session._workspace_path_policy().root_is_existing_host_path():
        return Path(cache_path.as_posix())
    return posix_path_as_path(cache_path)


def workspace_fingerprint_skip_relpaths(session: BaseSandboxSession) -> set[Path]:
    skip_paths = session._persist_workspace_skip_relpaths()
    skip_paths.update(session._workspace_resume_mount_skip_relpaths())
    return skip_paths


async def compute_and_cache_snapshot_fingerprint(
    session: BaseSandboxSession,
) -> dict[str, str]:
    helper_path = await session._ensure_runtime_helper_installed(WORKSPACE_FINGERPRINT_HELPER)
    command = [
        str(helper_path),
        session._workspace_root_path().as_posix(),
        session._snapshot_fingerprint_version(),
        session._snapshot_fingerprint_cache_path().as_posix(),
        session._resume_manifest_digest(),
    ]
    command.extend(
        rel_path.as_posix()
        for rel_path in sorted(
            session._workspace_fingerprint_skip_relpaths(),
            key=lambda path: path.as_posix(),
        )
    )
    result = await session.exec(*command, shell=False)
    if not result.ok():
        raise ExecNonZeroError(result, command=("compute_workspace_fingerprint", *command[1:]))
    return parse_snapshot_fingerprint_record(result.stdout)


async def read_cached_snapshot_fingerprint(session: BaseSandboxSession) -> dict[str, str]:
    result = await session.exec(
        "cat",
        "--",
        session._snapshot_fingerprint_cache_path().as_posix(),
        shell=False,
    )
    if not result.ok():
        raise ExecNonZeroError(
            result,
            command=("cat", session._snapshot_fingerprint_cache_path().as_posix()),
        )
    return parse_snapshot_fingerprint_record(result.stdout)


def parse_snapshot_fingerprint_record(payload: bytes | bytearray | str) -> dict[str, str]:
    raw = payload.decode("utf-8") if isinstance(payload, bytes | bytearray) else payload
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("snapshot fingerprint payload must be a JSON object")
    fingerprint = data.get("fingerprint")
    version = data.get("version")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("snapshot fingerprint payload is missing `fingerprint`")
    if not isinstance(version, str) or not version:
        raise ValueError("snapshot fingerprint payload is missing `version`")
    return {"fingerprint": fingerprint, "version": version}


async def delete_cached_snapshot_fingerprint_best_effort(session: BaseSandboxSession) -> None:
    try:
        await session.exec(
            "rm",
            "-f",
            "--",
            session._snapshot_fingerprint_cache_path().as_posix(),
            shell=False,
        )
    except Exception:
        return


def snapshot_fingerprint_version() -> str:
    return SNAPSHOT_FINGERPRINT_VERSION


def resume_manifest_digest(session: BaseSandboxSession) -> str:
    manifest_payload = json.dumps(
        session.state.manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(manifest_payload).hexdigest()


async def clear_workspace_root_on_resume(session: BaseSandboxSession) -> None:
    skip_rel_paths = session._workspace_resume_mount_skip_relpaths()
    if any(rel_path in (Path(""), Path(".")) for rel_path in skip_rel_paths):
        return

    await session._clear_workspace_dir_on_resume_pruned(
        current_dir=session._workspace_root_path(),
        skip_rel_paths=skip_rel_paths,
    )


def workspace_resume_mount_skip_relpaths(session: BaseSandboxSession) -> set[Path]:
    root = session._workspace_root_path()
    skip_rel_paths: set[Path] = set()
    for _mount, mount_path in session.state.manifest.ephemeral_mount_targets():
        try:
            skip_rel_paths.add(mount_path.relative_to(root))
        except ValueError:
            continue
    return skip_rel_paths


async def clear_workspace_dir_on_resume_pruned(
    session: BaseSandboxSession,
    *,
    current_dir: Path,
    skip_rel_paths: set[Path],
) -> None:
    root = session._workspace_root_path()
    try:
        entries = await session.ls(current_dir)
    except ExecNonZeroError:
        # If the root or subtree doesn't exist (or isn't listable), treat it as empty and let
        # hydrate/apply create it as needed.
        return

    for entry in entries:
        child = Path(entry.path)
        try:
            child_rel = child.relative_to(root)
        except ValueError:
            await session.rm(child, recursive=True)
            continue

        if child_rel in skip_rel_paths:
            continue
        if any(child_rel in skip_rel_path.parents for skip_rel_path in skip_rel_paths):
            if entry.kind == EntryKind.DIRECTORY:
                await session._clear_workspace_dir_on_resume_pruned(
                    current_dir=child,
                    skip_rel_paths=skip_rel_paths,
                )
            else:
                await session.rm(child, recursive=True)
            continue
        # `parse_ls_la` filters "." and ".." already; remove everything else recursively.
        await session.rm(child, recursive=True)


def _close_best_effort(stream: io.IOBase) -> None:
    try:
        stream.close()
    except Exception:
        pass


__all__ = [
    "SNAPSHOT_FINGERPRINT_VERSION",
    "can_skip_snapshot_restore_on_resume",
    "clear_workspace_dir_on_resume_pruned",
    "clear_workspace_root_on_resume",
    "compute_and_cache_snapshot_fingerprint",
    "delete_cached_snapshot_fingerprint_best_effort",
    "live_workspace_matches_snapshot_on_resume",
    "parse_snapshot_fingerprint_record",
    "persist_snapshot",
    "read_cached_snapshot_fingerprint",
    "restore_snapshot_into_workspace_on_resume",
    "resume_manifest_digest",
    "snapshot_fingerprint_cache_path",
    "snapshot_fingerprint_version",
    "workspace_fingerprint_skip_relpaths",
    "workspace_resume_mount_skip_relpaths",
]
