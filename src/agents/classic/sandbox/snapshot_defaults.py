"""Shared local snapshot defaults with legacy module compatibility."""

from _agents_shared.snapshot import LocalSnapshotSpec as LocalSnapshotSpec
from _agents_shared.snapshot_defaults import (
    _DEFAULT_LOCAL_SNAPSHOT_SUBDIR as _DEFAULT_LOCAL_SNAPSHOT_SUBDIR,
    _DEFAULT_LOCAL_SNAPSHOT_TTL_SECONDS as _DEFAULT_LOCAL_SNAPSHOT_TTL_SECONDS,
    _first_absolute_windows_env_path as _first_absolute_windows_env_path,
    cleanup_stale_default_local_snapshots as cleanup_stale_default_local_snapshots,
    default_local_snapshot_base_dir as default_local_snapshot_base_dir,
    resolve_default_local_snapshot_spec as resolve_default_local_snapshot_spec,
)
