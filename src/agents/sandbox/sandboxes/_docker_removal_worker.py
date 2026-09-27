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
import resource
import stat
from collections.abc import Generator, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
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


@dataclass
class _Bindings:
    paths: list[str]
    fds: list[int]

    def validate(self) -> None:
        for path, fd in zip(self.paths, self.fds, strict=False):
            # Workspace precedence makes nested grants ordinary writable entries.
            if path.startswith(self.paths[0] + "/"):
                continue
            entry = os.stat(path, follow_symlinks=False)
            if _canonical(path) != path or (entry.st_dev, entry.st_ino) != _identity(fd):
                raise ValueError("bound_root_replaced")


@contextmanager
def _bind_paths(paths: list[str]) -> Iterator[_Bindings]:
    path_flag = getattr(os, "O_PATH", None)
    if path_flag is None:
        raise RuntimeError("Linux O_PATH is required")
    device = os.stat("/").st_dev
    with ExitStack() as resources:
        resolved_paths: list[str] = []
        fds: list[int] = []
        for path in paths:
            resolved = _canonical(path)
            if resolved == "/":
                raise ValueError("filesystem_root")
            fd = os.open(resolved, path_flag | os.O_NOFOLLOW)
            retained = False
            try:
                resources.callback(os.close, fd)
                retained = True
            finally:
                if not retained:
                    os.close(fd)
            entry = os.fstat(fd)
            # The service validates all shared mounts as read-only grants before binding.
            # The workspace itself must remain on the container's private root filesystem.
            if (not resolved_paths and entry.st_dev != device) or not (
                stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)
            ):
                raise ValueError("grant_requires_private_root_filesystem")
            resolved_paths.append(resolved)
            fds.append(fd)
        if not stat.S_ISDIR(os.fstat(fds[0]).st_mode):
            raise ValueError("workspace_requires_existing_directory")
        yield _Bindings(resolved_paths, fds)


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


def _accounts(path: str) -> Generator[list[str], None, None]:
    try:
        # The workload is paused, so reject special files before opening them.
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            raise ValueError("account_file_requires_regular_file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("account_file_requires_regular_file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            remaining = 1024 * 1024
            while line := stream.readline(remaining + 1):
                remaining -= len(line)
                if remaining < 0:
                    raise ValueError("account_file_too_large")
                # Extra fields make a record invalid without allocating one item per delimiter.
                yield line.rstrip("\r\n").split(":", 7)
    finally:
        os.close(fd)


def _user_ids(user: str) -> tuple[int, int, list[int]]:
    name, separator, group = user.partition(":")
    if name.isdecimal() and separator and group.isdecimal():
        return int(name), int(group), []
    match = None
    with closing(_accounts("/etc/passwd")) as users:
        for entry in users:
            if match is None and len(entry) == 7 and (entry[0] == name or entry[2] == name):
                match = entry
    if name.isdecimal():
        uid = int(name)
    elif match is not None:
        uid = int(match[2])
    else:
        raise ValueError("unknown_user")
    gid = int(match[3]) if match is not None else 0
    groups: list[int] = []
    found = None
    with closing(_accounts("/etc/group")) as entries:
        for entry in entries:
            if len(entry) != 4:
                continue
            if separator:
                if found is None and entry[0] == group:
                    found = entry
            elif match is not None and "," not in match[0] and f",{match[0]}," in f",{entry[3]},":
                groups.append(int(entry[2]))
    if separator:
        if group.isdecimal():
            gid = int(group)
        elif found is not None:
            gid = int(found[2])
        else:
            raise ValueError("unknown_group")
    return uid, gid, groups


def _remove_leaf(path: str, *, dir_fd: int | None = None) -> bool:
    try:
        entry = os.lstat(path, dir_fd=dir_fd)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(path, dir_fd=dir_fd)
        return True
    # Empty directories require no permission to search their contents.
    try:
        os.rmdir(path, dir_fd=dir_fd)
        return True
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
    return False


def _remove(path: str, *, max_entry_visits: int) -> None:
    remaining = max_entry_visits

    def remove_leaf(selected: str, *, dir_fd: int | None = None) -> bool:
        nonlocal remaining
        if remaining <= 0:
            raise OSError(errno.E2BIG, "removal_entry_limit")
        remaining -= 1
        return _remove_leaf(selected, dir_fd=dir_fd)

    if remove_leaf(path):
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    ancestors: list[str] = []
    current_fd = os.open(path, flags)
    try:
        while True:
            # Stream names relative to the pinned directory, even beyond PATH_MAX.
            # Close the scanner before descent; keep no descriptor stack or sibling list.
            with os.scandir(current_fd) as entries:
                child_directory = next(
                    (
                        child.name
                        for child in entries
                        if not remove_leaf(child.name, dir_fd=current_fd)
                    ),
                    None,
                )
            if child_directory is not None:
                parent_fd = current_fd
                current_fd = os.open(child_directory, flags, dir_fd=parent_fd)
                os.close(parent_fd)
                ancestors.append(child_directory)
            elif ancestors:
                child_fd = current_fd
                current_fd = os.open("..", flags, dir_fd=child_fd)
                os.close(child_fd)
                remove_leaf(ancestors.pop(), dir_fd=current_fd)
            elif remove_leaf(path):
                return
    finally:
        os.close(current_fd)


def _remove_as_user(
    path: str, user: str, *, max_entry_visits: int, max_cpu_seconds: int
) -> dict[str, Any]:
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
                resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
                uid, gid, groups = _user_ids(user)
                os.setgroups(groups)
                os.setgid(gid)
                os.setuid(uid)
                _remove(path, max_entry_visits=max_entry_visits)
                outcome: dict[str, Any] = {"ok": True}
            except Exception as exc:
                outcome = {
                    "ok": False,
                    "reason": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
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
    data: dict[str, Any] = json.loads(result)
    return data


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
    with ExitStack() as resources:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                operation = request["operation"]
                response: dict[str, Any] = {"ok": True}
                if operation == "bind" and bindings is None:
                    bindings = resources.enter_context(_bind_paths(request["paths"]))
                    response["paths"] = bindings.paths
                elif operation == "inspect" and bindings is not None:
                    requested_path = ""
                    bindings.validate()
                    workspace_alias = request.get("workspace_root")
                    if (
                        workspace_alias is not None
                        and _canonical(workspace_alias) != bindings.paths[0]
                    ):
                        raise ValueError("workspace_alias_changed")
                    selected, is_directory = _selected_path(request["path"])
                    requested_path = request["path"]
                    response.update(path=selected, is_directory=is_directory)
                elif operation == "remove" and requested_path:
                    # The workload stays paused; preserve user search permissions on aliases.
                    path, requested_path = requested_path, ""
                    response = _remove_as_user(
                        path,
                        request["user"],
                        max_entry_visits=request["max_entry_visits"],
                        max_cpu_seconds=request["max_cpu_seconds"],
                    )
                elif operation == "close":
                    break
                else:
                    raise ValueError("invalid_worker_operation")
            except Exception as exc:
                response = {
                    "ok": False,
                    "reason": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
