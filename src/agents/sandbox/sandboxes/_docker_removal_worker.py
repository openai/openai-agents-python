"""Trusted host worker. Run by DockerRemovalService, never through container exec.

Only standard-library code loaded before entering the container is used. The parent
holds the workload paused for each request. A transport failure must leave it paused.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import posixpath
import stat
import sys
from typing import Any


def _canonical(path: str) -> str:
    return os.path.realpath("/" + path.lstrip("/"), strict=True)


def _identity(fd: int) -> tuple[int, int]:
    entry = os.fstat(fd)
    return entry.st_dev, entry.st_ino


class _Bindings:
    def __init__(self, paths: list[str]) -> None:
        self.paths: list[str] = []
        self.fds: list[int] = []
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            raise RuntimeError("Linux O_PATH is required")
        device = os.stat("/").st_dev
        try:
            for path in paths:
                resolved = _canonical(path)
                if resolved == "/":
                    raise ValueError("filesystem_root")
                fd = os.open(resolved, path_flag | os.O_NOFOLLOW)
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
        for fd in self.fds:
            os.close(fd)
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
    if match is None and not name.isdecimal():
        raise ValueError("unknown_user")
    uid = int(name) if name.isdecimal() else int(match[2])  # type: ignore[index]
    gid = int(match[3]) if match is not None else 0
    groups: list[int] = []
    entries = _accounts("/etc/group")
    if separator:
        found = next((p for p in entries if len(p) == 4 and p[0] == group), None)
        if not group.isdecimal() and found is None:
            raise ValueError("unknown_group")
        gid = int(group) if group.isdecimal() else int(found[2])  # type: ignore[index]
    elif match is not None:
        groups = [int(p[2]) for p in entries if len(p) == 4 and match[0] in p[3].split(",")]
    return uid, gid, groups


def _remove(path: str) -> None:
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(path)
        return
    # An empty directory can be removed without permission to search the directory.
    try:
        os.rmdir(path)
        return
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
    with os.scandir(path) as entries:
        for child in entries:
            _remove(child.path)
    os.rmdir(path)


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
        try:
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
            _remove(path)
            outcome: dict[str, Any] = {"ok": True}
        except BaseException as exc:
            outcome = {"ok": False, "errno": getattr(exc, "errno", None)}
        os.write(write_fd, json.dumps(outcome).encode("utf-8"))
        os._exit(0)
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
    mount_fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
    root_fd = os.open(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        if libc.setns(mount_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "setns_failed")
        os.fchdir(root_fd)
        os.chroot(".")
        os.chdir("/")
    finally:
        os.close(mount_fd)
        os.close(root_fd)


def main() -> None:
    _enter_container(int(sys.argv[1]))
    bindings: _Bindings | None = None
    requested_path = ""
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
    if bindings is not None:
        bindings.close()


if __name__ == "__main__":
    main()
