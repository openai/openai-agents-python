"""Standard-library file operations for trusted UnixLocal callers and its user worker."""

from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("UnixLocal file operations are not supported on Windows.")

import grp
import io
import json
import os
import pwd
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_TRAVERSE_FLAGS = (
    getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY)) | os.O_DIRECTORY | os.O_NOFOLLOW
)


class _FileOps:
    """Operate on canonical absolute paths already authorized by the owning session."""

    @contextmanager
    def parent(
        self, path: Path, *, for_write: bool = False, create_parents: bool = False
    ) -> Iterator[tuple[int, str]]:
        fd = os.open("/", _TRAVERSE_FLAGS)
        try:
            for part in path.parts[1:-1]:
                try:
                    child_fd = os.open(part, _TRAVERSE_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child_fd = os.open(part, _TRAVERSE_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child_fd
            yield fd, path.name or "."
        finally:
            os.close(fd)

    def read(self, path: Path) -> io.IOBase:
        with self.parent(path) as (parent_fd, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def write(self, path: Path, stream: io.IOBase) -> None:
        with self.parent(path, for_write=True, create_parents=True) as (parent_fd, name):
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o666,
                dir_fd=parent_fd,
            )
        try:
            out = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with out:
            shutil.copyfileobj(stream, out)

    def mkdir(self, path: Path, *, parents: bool) -> None:
        with self.parent(path, for_write=True, create_parents=parents) as (parent_fd, name):
            try:
                os.mkdir(name, dir_fd=parent_fd)
            except FileExistsError:
                # exist_ok only applies to a directory, never to a replacement symlink.
                if not stat.S_ISDIR(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                    raise

    @contextmanager
    def directory(self, path: Path) -> Iterator[int]:
        with self.parent(path) as (parent_fd, name):
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            yield fd
        finally:
            os.close(fd)

    def rm(self, path: Path, *, recursive: bool) -> None:
        with self.parent(path, for_write=True) as (parent_fd, name):
            _remove_at(parent_fd, name, recursive=recursive)

    def listing(self, path: Path) -> list[dict[str, str | int]]:
        with self.parent(path) as (parent_fd, name):
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                return [_entry(path, entry)]
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            with os.scandir(fd) as entries:
                return [
                    _entry(path / entry.name, entry.stat(follow_symlinks=False))
                    for entry in entries
                ]
        finally:
            os.close(fd)


def _entry(path: Path, entry: os.stat_result) -> dict[str, str | int]:
    try:
        owner = pwd.getpwuid(entry.st_uid).pw_name
    except KeyError:
        owner = str(entry.st_uid)
    try:
        group = grp.getgrgid(entry.st_gid).gr_name
    except KeyError:
        group = str(entry.st_gid)
    if stat.S_ISDIR(entry.st_mode):
        kind = "directory"
    elif stat.S_ISREG(entry.st_mode):
        kind = "file"
    elif stat.S_ISLNK(entry.st_mode):
        kind = "symlink"
    else:
        kind = "other"
    return {
        "path": str(path),
        "mode": entry.st_mode,
        "owner": owner,
        "group": group,
        "size": entry.st_size,
        "kind": kind,
    }


def _remove_at(parent_fd: int, name: str, *, recursive: bool) -> None:
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    if recursive:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            with os.scandir(fd) as entries:
                for child in entries:
                    try:
                        _remove_at(fd, child.name, recursive=True)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _main() -> None:
    # The application supplies this code and an authorized path directly, never via the workspace.
    operation, raw_path = sys.argv[1:]
    files = _FileOps()
    path = Path(raw_path)
    if operation == "write":
        files.write(path, cast(io.IOBase, sys.stdin.buffer))
    elif operation == "ls":
        print(json.dumps(files.listing(path), ensure_ascii=True))
    else:
        raise ValueError("Unsupported UnixLocal file operation")


if __name__ == "__main__":
    _main()
