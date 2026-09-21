"""Trusted host worker. Run by DockerRemovalService, never through container exec.

Only standard-library code loaded before entering the container is used. The parent
holds the workload paused for each request. A transport failure must leave it paused.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("The Docker removal worker requires a Linux host.")

import ctypes
import errno
import json
import os
import posixpath
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any


def _canonical(path: str) -> str:
    return os.path.realpath("/" + path.lstrip("/"), strict=True)


def _identity(fd: int) -> tuple[int, int]:
    entry = os.fstat(fd)
    return entry.st_dev, entry.st_ino


@contextmanager
def _open_fd(path: str, flags: int) -> Iterator[int]:
    fd = os.open(path, flags)
    try:
        yield fd
    finally:
        os.close(fd)


class _Bindings:
    def __init__(self, paths: list[str]) -> None:
        self.paths: list[str] = []
        self.fds: list[int] = []
        self._resources = ExitStack()
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            raise RuntimeError("Linux O_PATH is required")
        device = os.stat("/").st_dev
        try:
            for path in paths:
                resolved = _canonical(path)
                if resolved == "/":
                    raise ValueError("filesystem_root")
                fd = self._resources.enter_context(_open_fd(resolved, path_flag | os.O_NOFOLLOW))
                self.fds.append(fd)
                entry = os.fstat(fd)
                if entry.st_dev != device or not (
                    stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)
                ):
                    raise ValueError("grant_requires_private_root_filesystem")
                self.paths.append(resolved)
            if not stat.S_ISDIR(os.fstat(self.fds[0]).st_mode):
                raise ValueError("workspace_requires_existing_directory")
        except BaseException:
            self.close()
            raise

    def validate(self) -> None:
        for path, fd in zip(self.paths, self.fds, strict=False):
            # Workspace precedence makes nested grants ordinary writable entries.
            if path.startswith(self.paths[0] + "/"):
                continue
            entry = os.stat(path, follow_symlinks=False)
            if _canonical(path) != path or (entry.st_dev, entry.st_ino) != _identity(fd):
                raise ValueError("bound_root_replaced")

    def close(self) -> None:
        self._resources.close()
        self.fds.clear()


def _selected_path(path: str) -> tuple[str, bool]:
    path = posixpath.normpath("/" + path.lstrip("/"))
    if path == "/":
        raise ValueError("filesystem_root")
    # Resolve the parent but preserve unlink semantics for a symlink leaf.
    parent, name = posixpath.split(path)
    try:
        target = posixpath.join(_canonical(parent), name)
        entry = os.lstat(target)
    except FileNotFoundError:
        return "", False
    return target, stat.S_ISDIR(entry.st_mode)


def _accounts(path: str) -> list[list[str]]:
    try:
        # The workload is paused, so reject special files before opening them.
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            raise ValueError("account_file_requires_regular_file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("account_file_requires_regular_file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("account_file_too_large")
        return [line.split(":") for line in content.splitlines()]
    finally:
        os.close(fd)


def _user_ids(user: str) -> tuple[int, int, list[int]]:
    name, separator, group = user.partition(":")
    if name.isdecimal() and separator and group.isdecimal():
        return int(name), int(group), []
    users = _accounts("/etc/passwd")
    match = next((p for p in users if len(p) == 7 and (p[0] == name or p[2] == name)), None)
    if name.isdecimal():
        uid = int(name)
    elif match is not None:
        uid = int(match[2])
    else:
        raise ValueError("unknown_user")
    gid = int(match[3]) if match is not None else 0
    groups: list[int] = []
    entries = _accounts("/etc/group")
    if separator:
        found = next((p for p in entries if len(p) == 4 and p[0] == group), None)
        if group.isdecimal():
            gid = int(group)
        elif found is not None:
            gid = int(found[2])
        else:
            raise ValueError("unknown_group")
    elif match is not None:
        groups = [int(p[2]) for p in entries if len(p) == 4 and match[0] in p[3].split(",")]
    return uid, gid, groups


def _remove(path: str) -> None:
    pending = [(path, False)]
    while pending:
        current, children_removed = pending.pop()
        if children_removed:
            os.rmdir(current)
            continue
        try:
            entry = os.lstat(current)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(entry.st_mode):
            os.unlink(current)
            continue
        # An empty directory can be removed without permission to search the directory.
        try:
            os.rmdir(current)
            continue
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
        pending.append((current, True))
        with os.scandir(current) as entries:
            pending.extend((child.path, False) for child in entries)


def _remove_as_user(path: str, user: str) -> None:
    uid, gid, groups = _user_ids(user)
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        os.close(read_fd)
        exit_code = 1
        try:
            try:
                os.setgroups(groups)
                os.setgid(gid)
                os.setuid(uid)
                _remove(path)
                outcome: dict[str, Any] = {"ok": True}
            except Exception as exc:
                outcome = {"ok": False, "errno": getattr(exc, "errno", None)}
            os.write(write_fd, json.dumps(outcome).encode("utf-8"))
            exit_code = 0
        finally:
            # The child must never resume the parent's request loop, even on interruption.
            os._exit(exit_code)
    os.close(write_fd)
    try:
        result = os.read(read_fd, 4096)
        _, wait_status = os.waitpid(pid, 0)
    finally:
        os.close(read_fd)
    if wait_status != 0 or not result:
        raise RuntimeError("removal_worker_failed")
    data = json.loads(result)
    if not data["ok"]:
        raise OSError(data["errno"] or 1, "removal_failed")


def _enter_container(pid: int) -> None:
    # Load host libc before changing the filesystem namespace or root.
    libc = ctypes.CDLL(None, use_errno=True)
    with (
        _open_fd(f"/proc/{pid}/ns/mnt", os.O_RDONLY) as mount_fd,
        _open_fd(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY) as root_fd,
    ):
        if libc.setns(mount_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "setns_failed")
        os.fchdir(root_fd)
        os.chroot(".")
        os.chdir("/")


def main() -> None:
    _enter_container(int(sys.argv[1]))
    bindings: _Bindings | None = None
    requested_path = ""
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                operation = request["operation"]
                response: dict[str, Any] = {"ok": True}
                if operation == "bind" and bindings is None:
                    bindings = _Bindings(request["paths"])
                    response["paths"] = bindings.paths
                elif operation == "inspect" and bindings is not None:
                    requested_path = ""
                    bindings.validate()
                    selected, is_directory = _selected_path(request["path"])
                    requested_path = request["path"]
                    response.update(path=selected, is_directory=is_directory)
                elif operation == "remove" and requested_path:
                    # The workload stays paused; preserve user search permissions on aliases.
                    path, requested_path = requested_path, ""
                    _remove_as_user(path, request["user"])
                elif operation == "close":
                    break
                else:
                    raise ValueError("invalid_worker_operation")
            except Exception as exc:
                response = {"ok": False, "reason": type(exc).__name__}
            print(json.dumps(response), flush=True)
    finally:
        if bindings is not None:
            bindings.close()


if __name__ == "__main__":
    main()
