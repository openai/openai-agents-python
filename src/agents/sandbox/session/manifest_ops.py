from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..entries import BaseEntry
from ..materialization import MaterializationResult, MaterializedFile
from .manifest_application import ManifestApplier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .base_sandbox_session import BaseSandboxSession


async def apply_manifest(
    session: BaseSandboxSession,
    *,
    only_ephemeral: bool = False,
    provision_accounts: bool = True,
) -> MaterializationResult:
    applier = _build_manifest_applier(session, include_entry_concurrency=True)
    return await applier.apply_manifest(
        session.state.manifest,
        only_ephemeral=only_ephemeral,
        provision_accounts=provision_accounts,
        base_dir=session._manifest_base_dir(),
    )


async def provision_manifest_accounts(session: BaseSandboxSession) -> None:
    applier = _build_manifest_applier(session, include_entry_concurrency=False)
    await applier.provision_accounts(session.state.manifest)


async def apply_entry_batch(
    session: BaseSandboxSession,
    entries: Sequence[tuple[Path, BaseEntry]],
    *,
    base_dir: Path,
) -> list[MaterializedFile]:
    applier = _build_manifest_applier(session, include_entry_concurrency=True)
    return await applier._apply_entry_batch(entries, base_dir=base_dir)


def _build_manifest_applier(
    session: BaseSandboxSession,
    *,
    include_entry_concurrency: bool,
) -> ManifestApplier:
    max_entry_concurrency = (
        session._max_manifest_entry_concurrency if include_entry_concurrency else None
    )
    return ManifestApplier(
        mkdir=lambda path: session.mkdir(path, parents=True),
        exec_checked_nonzero=session._exec_checked_nonzero,
        apply_entry=lambda artifact, dest, base_dir: artifact.apply(session, dest, base_dir),
        max_entry_concurrency=max_entry_concurrency,
    )


__all__ = [
    "apply_entry_batch",
    "apply_manifest",
    "provision_manifest_accounts",
]
