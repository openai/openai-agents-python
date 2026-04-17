from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .types import Permissions


class EntryKind(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True, kw_only=True)
class FileEntry:
    path: str
    permissions: Permissions
    owner: str
    group: str
    size: int
    kind: EntryKind = EntryKind.FILE

    def is_dir(self) -> bool:
        return self.kind == EntryKind.DIRECTORY
