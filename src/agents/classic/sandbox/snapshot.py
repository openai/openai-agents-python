"""Compatibility aliases for the shared implementation."""

import sys as _sys

from _agents_shared import snapshot as _implementation
from _agents_shared.snapshot import (
    LocalSnapshot as LocalSnapshot,
    LocalSnapshotSpec as LocalSnapshotSpec,
    NoopSnapshot as NoopSnapshot,
    NoopSnapshotSpec as NoopSnapshotSpec,
    RemoteSnapshot as RemoteSnapshot,
    RemoteSnapshotSpec as RemoteSnapshotSpec,
    SnapshotBase as SnapshotBase,
    SnapshotClass as SnapshotClass,
    SnapshotSpec as SnapshotSpec,
    SnapshotSpecUnion as SnapshotSpecUnion,
    _maybe_await as _maybe_await,
    resolve_snapshot as resolve_snapshot,
)

_sys.modules[__name__] = _implementation
