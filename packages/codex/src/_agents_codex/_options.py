"""Native environment and recorded permission shapes."""

from collections.abc import Sequence as Sequence
from typing import Literal as Literal

from typing_extensions import Required as Required, TypedDict as TypedDict

from agents.config import FileSystemAccessMode as FileSystemAccessMode

from ._public import preserve_module_identity as _preserve_module_identity


class RuntimeNetworkPolicy(TypedDict, total=False, closed=True):
    allowed_domains: Sequence[str]
    denied_domains: Sequence[str]
    allow_all_unix_sockets: bool
    allow_local_binding: bool


class RuntimeStorageMount(TypedDict, closed=True):
    source: str
    destination: str
    strategy: Literal["local_bind"]
    read_only: bool


class RuntimeEnvironment(TypedDict, total=False, closed=True):
    """Select the local executor or connect to WSS with a timeout in seconds."""

    url: str
    local: bool
    read_only: bool
    cwd: Required[str]
    headers: dict[str, str]
    env: dict[str, str]
    connect_timeout: float | None
    local_mcp: bool
    network: RuntimeNetworkPolicy
    capability_directories: list[str]  # Remote file: URIs; omitted inherits, [] clears.
    writable_roots: list[str]  # Additional write roots, as executor file: URIs.
    mounts: list[RuntimeStorageMount]


class BasicSpecialPath(TypedDict):
    kind: Literal["root", "minimal", "tmpdir", "slash_tmp"]


class ProjectRootsPath(TypedDict, total=False):
    kind: Required[Literal["project_roots", "current_working_directory"]]
    subpath: str | None


class UnknownSpecialPath(TypedDict, total=False):
    kind: Required[Literal["unknown"]]
    path: Required[str]
    subpath: str | None


class LiteralFileSystemPath(TypedDict):
    type: Literal["path"]
    path: str


class GlobFileSystemPath(TypedDict):
    type: Literal["glob_pattern"]
    pattern: str


class SpecialFileSystemPath(TypedDict):
    type: Literal["special"]
    value: BasicSpecialPath | ProjectRootsPath | UnknownSpecialPath


class FileSystemEntry(TypedDict, total=False):
    path: Required[LiteralFileSystemPath | GlobFileSystemPath | SpecialFileSystemPath]
    access: Required[FileSystemAccessMode | Literal["none"]]
    missing_path_behavior: Literal["skip"] | None


class LegacyNetworkPermissions(TypedDict, total=False):
    enabled: bool | None


class FileSystemPermissions(TypedDict, total=False):
    entries: list[FileSystemEntry]
    glob_scan_max_depth: int | None


class LegacyFileSystemPermissions(TypedDict, total=False):
    read: list[str] | None
    write: list[str] | None


class LegacyPermissionProfile(TypedDict, total=False):
    network: LegacyNetworkPermissions | None
    file_system: FileSystemPermissions | LegacyFileSystemPermissions | None


_preserve_module_identity(globals(), "agents._options")
