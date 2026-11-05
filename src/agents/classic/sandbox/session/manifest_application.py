from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from ...run_config import DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY
from ..entries import BaseEntry, Dir, Mount, resolve_workspace_path
from ..manifest import Manifest
from ..materialization import MaterializationResult, MaterializedFile, gather_in_order
from ..types import ExecResult, User
from ..workspace_paths import coerce_posix_path, posix_path_as_path


class ManifestApplier:
    def __init__(
        self,
        *,
        mkdir: Callable[[Path], Awaitable[None]],
        exec_checked_nonzero: Callable[..., Awaitable[ExecResult]],
        apply_entry: Callable[[BaseEntry, Path, Path], Awaitable[list[MaterializedFile]]],
        max_entry_concurrency: int | None = DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY,
    ) -> None:
        if max_entry_concurrency is not None and max_entry_concurrency < 1:
            raise ValueError("max_entry_concurrency must be at least 1")
        self._mkdir = mkdir
        self._exec_checked_nonzero = exec_checked_nonzero
        self._apply_entry = apply_entry
        self._max_entry_concurrency = max_entry_concurrency

    async def apply_manifest(
        self,
        manifest: Manifest,
        *,
        only_ephemeral: bool = False,
        provision_accounts: bool = True,
        base_dir: Path | None = None,
    ) -> MaterializationResult:
        base_dir = posix_path_as_path(coerce_posix_path("/")) if base_dir is None else base_dir
        root = posix_path_as_path(coerce_posix_path(manifest.root))

        await self._mkdir(root)

        if provision_accounts and not only_ephemeral:
            await self.provision_accounts(manifest)

        entries_to_apply: list[tuple[Path, BaseEntry]] = []
        if only_ephemeral:
            for rel_dest, artifact in self._ephemeral_entries(manifest):
                dest = resolve_workspace_path(root, rel_dest)
                entries_to_apply.append((dest, artifact))
        else:
            for raw_rel_dest, artifact in manifest.validated_entries().items():
                dest = resolve_workspace_path(
                    root,
                    Manifest._coerce_rel_path(raw_rel_dest),
                )
                entries_to_apply.append((dest, artifact))

        return MaterializationResult(
            files=await self._apply_entry_batch(entries_to_apply, base_dir=base_dir),
        )

    async def provision_accounts(self, manifest: Manifest) -> None:
        all_users: set[User] = set(manifest.users)
        for group in manifest.groups:
            all_users |= set(group.users)
            await self._exec_checked_nonzero("groupadd", group.name)

        for user in all_users:
            await self._exec_checked_nonzero(
                "useradd",
                "-U",
                "-M",
                "-s",
                "/usr/sbin/nologin",
                user.name,
            )

        for group in manifest.groups:
            for user in group.users:
                await self._exec_checked_nonzero("usermod", "-aG", group.name, user.name)

    def _ephemeral_entries(self, manifest: Manifest) -> list[tuple[Path, BaseEntry]]:
        entries: list[tuple[Path, BaseEntry]] = []
        for rel_dest, artifact in manifest.entries.items():
            self._collect_ephemeral_entries(
                rel_dest=Manifest._coerce_rel_path(rel_dest),
                artifact=artifact,
                out=entries,
            )
        return entries

    def _collect_ephemeral_entries(
        self,
        *,
        rel_dest: Path,
        artifact: BaseEntry,
        out: list[tuple[Path, BaseEntry]],
    ) -> None:
        manifest_rel = Manifest._coerce_rel_path(rel_dest)
        Manifest._validate_rel_path(manifest_rel)
        if artifact.ephemeral:
            out.append((manifest_rel, self._prune_to_ephemeral(artifact)))
            return
        if isinstance(artifact, Dir):
            for child_name, child_artifact in artifact.children.items():
                self._collect_ephemeral_entries(
                    rel_dest=manifest_rel / Manifest._coerce_rel_path(child_name),
                    artifact=child_artifact,
                    out=out,
                )

    def _prune_to_ephemeral(self, artifact: BaseEntry) -> BaseEntry:
        if not isinstance(artifact, Dir):
            return artifact
        if artifact.ephemeral:
            return artifact.model_copy(deep=True)

        pruned_children: dict[str | Path, BaseEntry] = {}
        for child_name, child_artifact in artifact.children.items():
            if child_artifact.ephemeral:
                pruned_children[child_name] = self._prune_to_ephemeral(child_artifact)
                continue
            if isinstance(child_artifact, Dir):
                nested = self._prune_to_ephemeral(child_artifact)
                if isinstance(nested, Dir) and nested.children:
                    pruned_children[child_name] = nested

        return artifact.model_copy(update={"children": pruned_children}, deep=True)

    @staticmethod
    def _paths_overlap(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    async def _apply_entry_batch(
        self,
        entries: Sequence[tuple[Path, BaseEntry]],
        *,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        files: list[MaterializedFile] = []
        parallel_batch: list[tuple[Path, BaseEntry]] = []

        async def _flush_parallel_batch() -> None:
            nonlocal files
            if not parallel_batch:
                return

            def _make_apply_task(
                dest: Path,
                artifact: BaseEntry,
            ) -> Callable[[], Awaitable[list[MaterializedFile]]]:
                async def _apply() -> list[MaterializedFile]:
                    return await self._apply_entry(artifact, dest, base_dir)

                return _apply

            batch = list(parallel_batch)
            parallel_batch.clear()
            batch_files = await gather_in_order(
                [_make_apply_task(dest, artifact) for dest, artifact in batch],
                max_concurrency=self._max_entry_concurrency,
            )
            for entry_files in batch_files:
                files.extend(entry_files)

        for dest, artifact in entries:
            if isinstance(artifact, Mount) or any(
                self._paths_overlap(dest, queued_dest) for queued_dest, _ in parallel_batch
            ):
                await _flush_parallel_batch()
                files.extend(await self._apply_entry(artifact, dest, base_dir))
                continue

            parallel_batch.append((dest, artifact))

        await _flush_parallel_batch()
        return files
