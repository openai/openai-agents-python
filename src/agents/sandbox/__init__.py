from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..run_config import SandboxArchiveLimits, SandboxConcurrencyLimits, SandboxRunConfig
    from .capabilities import Capability
    from .config import MemoryGenerateConfig, MemoryLayoutConfig, MemoryReadConfig
    from .entries import Dir, LocalFile
    from .errors import (
        ErrorCode,
        ExecTimeoutError,
        ExecTransportError,
        ExposedPortUnavailableError,
        SandboxError,
        WorkspaceArchiveReadError,
        WorkspaceArchiveWriteError,
        WorkspaceReadNotFoundError,
        WorkspaceWriteTypeError,
    )
    from .manifest import Manifest
    from .sandbox_agent import SandboxAgent
    from .snapshot import (
        LocalSnapshot,
        LocalSnapshotSpec,
        RemoteSnapshot,
        RemoteSnapshotSpec,
        SnapshotSpec,
        resolve_snapshot,
    )
    from .types import ExecResult, ExposedPortEndpoint, FileMode, Group, Permissions, User
    from .workspace_paths import SandboxPathGrant, SandboxWorkspaceScope

_LAZY_EXPORTS = {
    "SandboxArchiveLimits": "..run_config",
    "SandboxConcurrencyLimits": "..run_config",
    "SandboxRunConfig": "..run_config",
    "Capability": ".capabilities",
    "MemoryGenerateConfig": ".config",
    "MemoryLayoutConfig": ".config",
    "MemoryReadConfig": ".config",
    "Dir": ".entries",
    "LocalFile": ".entries",
    "ErrorCode": ".errors",
    "ExecTimeoutError": ".errors",
    "ExecTransportError": ".errors",
    "ExposedPortUnavailableError": ".errors",
    "SandboxError": ".errors",
    "WorkspaceArchiveReadError": ".errors",
    "WorkspaceArchiveWriteError": ".errors",
    "WorkspaceReadNotFoundError": ".errors",
    "WorkspaceWriteTypeError": ".errors",
    "Manifest": ".manifest",
    "SandboxAgent": ".sandbox_agent",
    "LocalSnapshot": ".snapshot",
    "LocalSnapshotSpec": ".snapshot",
    "RemoteSnapshot": ".snapshot",
    "RemoteSnapshotSpec": ".snapshot",
    "SnapshotSpec": ".snapshot",
    "resolve_snapshot": ".snapshot",
    "ExecResult": ".types",
    "ExposedPortEndpoint": ".types",
    "FileMode": ".types",
    "Group": ".types",
    "Permissions": ".types",
    "User": ".types",
    "SandboxPathGrant": ".workspace_paths",
    "SandboxWorkspaceScope": ".workspace_paths",
}


__all__ = [
    "Capability",
    "Dir",
    "ErrorCode",
    "ExecResult",
    "ExposedPortEndpoint",
    "ExposedPortUnavailableError",
    "ExecTimeoutError",
    "ExecTransportError",
    "FileMode",
    "Group",
    "LocalFile",
    "LocalSnapshot",
    "LocalSnapshotSpec",
    "Manifest",
    "MemoryLayoutConfig",
    "MemoryReadConfig",
    "MemoryGenerateConfig",
    "RemoteSnapshot",
    "RemoteSnapshotSpec",
    "Permissions",
    "SandboxAgent",
    "SandboxArchiveLimits",
    "SandboxPathGrant",
    "SandboxWorkspaceScope",
    "SandboxConcurrencyLimits",
    "SandboxError",
    "SandboxRunConfig",
    "SnapshotSpec",
    "WorkspaceArchiveReadError",
    "WorkspaceArchiveWriteError",
    "WorkspaceReadNotFoundError",
    "WorkspaceWriteTypeError",
    "User",
    "resolve_snapshot",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
