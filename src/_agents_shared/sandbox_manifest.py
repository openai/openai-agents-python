from __future__ import annotations

import asyncio
from pathlib import Path

from agents.classic.sandbox.entries import BaseEntry, Dir, Mount
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession


class _BorrowedManifestPolicy:
    @staticmethod
    def _host_path_grant_topology(
        manifest: Manifest,
    ) -> tuple[tuple[str, bool, str | None], ...]:
        mounted_targets = {
            grant.path for grant in manifest.extra_path_grants if grant.host_path is not None
        }
        return tuple(
            (grant.path, grant.read_only, grant.host_path)
            for grant in manifest.extra_path_grants
            if grant.path in mounted_targets
        )

    @classmethod
    def _validate_live_session_host_path_grants(
        cls,
        *,
        current_manifest: Manifest,
        processed_manifest: Manifest,
    ) -> None:
        if cls._host_path_grant_topology(current_manifest) != cls._host_path_grant_topology(
            processed_manifest
        ):
            raise ValueError(
                "Injected sandbox sessions do not support capability changes to host-backed "
                "`manifest.extra_path_grants`; use a fresh session or a session_state resume flow."
            )

        # Generic provider batches cannot roll back partial writes or revoke
        # older snapshots that still target this mutable resource. Only a
        # provider transaction or an independently owned layer can admit changes.
        cls._validate_running_live_session_manifest_update(
            current_manifest=current_manifest, processed_manifest=processed_manifest
        )
        if cls._diff_live_session_entries(
            current_entries=current_manifest.entries,
            processed_entries=processed_manifest.entries,
        ):
            raise ValueError(
                "Borrowed sandbox manifest entry changes require an atomic provider transaction; "
                "use a fresh owned sandbox or snapshot creation"
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
        if _BorrowedManifestPolicy._entry_contains_mount(entry):
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
                _BorrowedManifestPolicy._entry_contains_mount(child)
                for child in entry.children.values()
            )
        return False


def _borrowed_manifest_owner(owner: BaseSandboxSession) -> BaseSandboxSession:
    from agents.classic.sandbox.session.sandbox_session import SandboxSession

    while isinstance(owner, SandboxSession):
        owner = owner._inner
    return owner


def _assert_borrowed_manifest_reusable(owner: BaseSandboxSession) -> None:
    if getattr(_borrowed_manifest_owner(owner), "_borrowed_manifest_failed", False):
        raise ValueError(
            "Borrowed sandbox manifest application failed; create a fresh sandbox resource"
        )


def _borrowed_manifest_lock(owner: BaseSandboxSession) -> asyncio.Lock:
    owner = _borrowed_manifest_owner(owner)
    lock: asyncio.Lock | None = getattr(owner, "_borrowed_manifest_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(owner, "_borrowed_manifest_lock", lock)  # noqa: B010
    return lock
