from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from .types import Permissions


class EntryKind(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    OTHER = "other"


class FileEntry(BaseModel):
    path: str
    permissions: Permissions
    owner: str
    group: str
    size: int
    kind: EntryKind = EntryKind.FILE

    def is_dir(self) -> bool:
        return self.kind == EntryKind.DIRECTORY
