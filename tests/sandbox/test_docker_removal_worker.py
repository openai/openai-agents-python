"""Trusted worker tests use syscall and stream doubles without privileged operations."""

from __future__ import annotations

import errno
import io
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from agents.sandbox import Manifest, Permissions, SandboxPathGrant
from agents.sandbox.errors import WorkspaceArchiveWriteError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.sandboxes import (
    docker_removal,
)
from agents.sandbox.sandboxes.docker_removal import _Worker

from . import _docker_removal_helpers as removal_helpers
from ._docker_removal_helpers import (
    manifest,
    session,
)

service = removal_helpers.service
worker_code = pytest.importorskip(
    "agents.sandbox.sandboxes._docker_removal_worker", exc_type=ImportError
)


def test_empty_directory_needs_no_search_of_its_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_code.os, "lstat", lambda _, **kwargs: SimpleNamespace(st_mode=0o040000)
    )
    removed: list[str] = []
    monkeypatch.setattr(worker_code.os, "rmdir", lambda path, **kwargs: removed.append(path))
    monkeypatch.setattr(
        worker_code.os, "scandir", Mock(side_effect=AssertionError("must not search"))
    )
    worker_code._remove("/workspace/empty", max_entry_visits=100_000)
    assert removed == ["/workspace/empty"]


@pytest.mark.parametrize("path", ["/", "//", "///", "/workspace/.."])
def test_worker_refuses_filesystem_root_without_filesystem_calls(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker_code.os, "lstat", Mock(side_effect=AssertionError("must not inspect root"))
    )
    with pytest.raises(ValueError, match="filesystem_root"):
        worker_code._selected_path(path)


def test_replaced_bound_root_is_rejected_even_through_new_parent_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = object.__new__(worker_code._Bindings)
    bindings.paths, bindings.fds = ["/data/protected"], [123]
    same_inode = SimpleNamespace(st_dev=1, st_ino=2)
    monkeypatch.setattr(worker_code.os, "stat", lambda *args, **kwargs: same_inode)
    monkeypatch.setattr(worker_code.os, "fstat", lambda _: same_inode)
    monkeypatch.setattr(worker_code, "_canonical", lambda _: "/elsewhere/protected")
    with pytest.raises(ValueError, match="bound_root_replaced"):
        bindings.validate()


@pytest.mark.parametrize("workspace_device", [1, 2])
def test_worker_pins_external_mount_device_but_requires_private_workspace(
    monkeypatch: pytest.MonkeyPatch, workspace_device: int
) -> None:
    metadata = {
        20: SimpleNamespace(st_dev=workspace_device, st_ino=10, st_mode=stat.S_IFDIR),
        21: SimpleNamespace(st_dev=2, st_ino=11, st_mode=stat.S_IFDIR),
    }
    closed: list[int] = []
    monkeypatch.setattr(worker_code.os, "O_PATH", 0, raising=False)
    monkeypatch.setattr(worker_code, "_canonical", lambda path: path)
    monkeypatch.setattr(
        worker_code.os,
        "stat",
        lambda path, **kwargs: SimpleNamespace(st_dev=1)
        if path == "/"
        else metadata[20 if path == "/workspace" else 21],
    )
    monkeypatch.setattr(worker_code.os, "open", Mock(side_effect=[20, 21]))
    monkeypatch.setattr(worker_code.os, "fstat", metadata.__getitem__)
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    if workspace_device != 1:
        with pytest.raises(ValueError, match="private_root_filesystem"):
            with worker_code._bind_paths(["/workspace", "/toolchain"]):
                pytest.fail("workspace must remain private")
        assert closed == [20]
    else:
        with worker_code._bind_paths(["/workspace", "/toolchain"]) as bindings:
            bindings.validate()
            assert bindings.fds == [20, 21]
        assert closed == [21, 20]


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("developer", (1000, 1001, [2000])),
        ("1000", (1000, 1001, [2000])),
        ("1000:3000", (1000, 3000, [])),
        ("developer:tools", (1000, 2000, [])),
        ("developer:3000", (1000, 3000, [])),
    ],
)
def test_requested_user_and_groups_are_preserved(
    user: str, expected: tuple[int, int, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = {
        "/etc/passwd": [["developer", "x", "1000", "1001", "", "/home/developer", "/bin/sh"]],
        "/etc/group": [["tools", "x", "2000", "developer"]],
    }
    monkeypatch.setattr(worker_code, "_accounts", lambda path: (entry for entry in accounts[path]))
    assert worker_code._user_ids(user) == expected


def test_namespace_entry_uses_only_mount_namespace_and_closes_host_directory_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        worker_code.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            setns=lambda fd, kind: calls.append(("setns", fd, kind)) or 0
        ),
    )
    monkeypatch.setattr(
        worker_code.os, "open", lambda path, flags: 20 if path.endswith("mnt") else 21
    )
    for method in ("fchdir", "chroot", "chdir", "close"):
        monkeypatch.setattr(
            worker_code.os, method, lambda value, method=method: calls.append((method, value))
        )
    worker_code._enter_container(123)
    assert calls[:4] == [
        ("setns", 20, 0),
        ("fchdir", 21),
        ("chroot", "."),
        ("chdir", "/"),
    ]
    assert sorted(calls[4:]) == [("close", 20), ("close", 21)]


@pytest.mark.parametrize("accessible", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_worker_protocol_keeps_original_traversal_for_user_scoped_removal(
    accessible: bool, exists: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the protocol in memory; namespaces, credentials and syscalls are doubles.
    original = "/workspace/private/link/build"
    canonical = "/workspace/shared/build"
    requests = [
        {"operation": "bind", "paths": ["/workspace"]},
        {"operation": "inspect", "path": original},
        {
            "operation": "remove",
            "user": "1000:1000",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    output = io.StringIO()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests))))
    monkeypatch.setattr(worker_code.sys, "stdout", output)
    monkeypatch.setattr(worker_code, "_enter_container", lambda _: None)
    monkeypatch.setattr(
        worker_code,
        "_bind_paths",
        lambda paths: nullcontext(SimpleNamespace(paths=paths, validate=lambda: None)),
    )
    monkeypatch.setattr(worker_code, "_canonical", lambda _: "/workspace/shared")
    current_user = "root"
    removed: list[str] = []

    def lstat(path: str, *, dir_fd: int | None = None) -> SimpleNamespace:
        if current_user != "root" and path == original and not accessible:
            raise PermissionError("ancestor denies search")
        if not exists:
            raise FileNotFoundError("missing leaf")
        return SimpleNamespace(st_mode=0o040755)

    def remove_as_user(path: str, user: str, **limits: int) -> dict[str, Any]:
        nonlocal current_user
        current_user = user
        worker_code._remove(path, max_entry_visits=100_000)
        return {"ok": True}

    monkeypatch.setattr(worker_code.os, "lstat", lstat)
    monkeypatch.setattr(worker_code.os, "rmdir", lambda path, **kwargs: removed.append(path))
    monkeypatch.setattr(worker_code, "_remove_as_user", remove_as_user)
    worker_code.main()
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[1] == {
        "ok": True,
        "path": canonical if exists else "",
        "is_directory": exists,
    }
    assert responses[2] == (
        {"ok": True} if accessible else {"ok": False, "reason": "PermissionError", "errno": None}
    )
    assert removed == ([original] if accessible and exists else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "error_number"),
    [("PermissionError", errno.EACCES), ("OSError", errno.E2BIG), ("ValueError", None)],
)
async def test_child_failure_reaches_structured_session_error(
    service: Any, monkeypatch: pytest.MonkeyPatch, reason: str, error_number: int | None
) -> None:
    manager, container, _ = service
    configured = manifest()
    requests = [
        {"operation": "bind", "paths": ["/workspace", "/external", "/grant-alias"]},
        {"operation": "inspect", "path": "/workspace/build"},
        {
            "operation": "remove",
            "user": "developer",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    output = io.StringIO()
    child_response = {"ok": False, "reason": reason, "errno": error_number}
    with monkeypatch.context() as worker_patch:
        worker_patch.setattr(worker_code.sys, "argv", ["worker", "123"])
        worker_patch.setattr(
            worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests)))
        )
        worker_patch.setattr(worker_code.sys, "stdout", output)
        worker_patch.setattr(worker_code, "_enter_container", lambda _: None)
        worker_patch.setattr(
            worker_code,
            "_bind_paths",
            lambda paths: nullcontext(SimpleNamespace(paths=paths, validate=lambda: None)),
        )
        worker_patch.setattr(worker_code, "_selected_path", lambda path: (path, True))
        worker_patch.setattr(worker_code.os, "pipe", lambda: (20, 21))
        worker_patch.setattr(worker_code.os, "fork", lambda: 123)
        worker_patch.setattr(
            worker_code.os, "read", lambda fd, size: json.dumps(child_response).encode()
        )
        worker_patch.setattr(worker_code.os, "waitpid", lambda pid, options: (pid, 0))
        worker_patch.setattr(worker_code.os, "close", lambda fd: None)
        worker_code.main()

    worker = object.__new__(_Worker)
    worker.process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(output.getvalue()))
    worker.uncertain = False
    monkeypatch.setattr(docker_removal, "_Worker", lambda _: worker)
    manager.bind_new(container, configured)
    with pytest.raises(WorkspaceArchiveWriteError) as caught:
        await session(manager, container, configured).rm("build", recursive=True, user="developer")
    assert caught.value.context["reason"] == "docker_removal_failed"
    assert caught.value.context["worker_reason"] == reason
    assert caught.value.context["errno"] == error_number
    assert isinstance(caught.value.cause, OSError)
    assert caught.value.cause.errno == error_number
    assert not worker.uncertain
    assert not container.attrs["State"]["Paused"]


@pytest.mark.parametrize("path", ["/etc/passwd", "/etc/group"])
@pytest.mark.parametrize("kind", [stat.S_IFCHR, stat.S_IFIFO, stat.S_IFLNK])
def test_account_special_files_are_rejected_before_open(
    path: str, kind: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = Mock(return_value=SimpleNamespace(st_mode=kind | 0o644))
    open_file = Mock(side_effect=AssertionError("must not open special files"))
    monkeypatch.setattr(worker_code.os, "stat", metadata)
    monkeypatch.setattr(worker_code.os, "open", open_file)
    with pytest.raises(ValueError, match="account_file_requires_regular_file"):
        list(worker_code._accounts(path))
    metadata.assert_called_once_with(path, follow_symlinks=False)
    open_file.assert_not_called()


def test_regular_account_file_is_checked_before_and_after_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def metadata(*args: Any, **kwargs: Any) -> SimpleNamespace:
        events.append("stat")
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

    monkeypatch.setattr(worker_code.os, "stat", metadata)
    monkeypatch.setattr(worker_code.os, "open", lambda *args: events.append("open") or 20)
    monkeypatch.setattr(worker_code.os, "fstat", metadata)
    monkeypatch.setattr(
        worker_code.os,
        "fdopen",
        lambda *args, **kwargs: io.StringIO("developer:x:1000:1000::/home/developer:/bin/sh\n"),
    )
    monkeypatch.setattr(worker_code.os, "close", lambda _: events.append("close"))
    assert list(worker_code._accounts("/etc/passwd")) == [
        ["developer", "x", "1000", "1000", "", "/home/developer", "/bin/sh"]
    ]
    assert events == ["stat", "open", "stat", "close"]


@pytest.mark.parametrize("shape", ["newlines", "fields", "members", "oversized"])
def test_user_lookup_bounds_account_parsing(shape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class AccountField(str):
        def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
            if maxsplit < 0:
                raise AssertionError("account fields must not expand into unbounded lists")
            return super().split(sep, maxsplit)

    class AccountLine(str):
        def rstrip(self, chars: str | None = None) -> AccountLine:
            return AccountLine(super().rstrip(chars))

        def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
            if not 0 <= maxsplit <= 8:
                raise AssertionError("account records require bounded field splitting")
            return [AccountField(field) for field in super().split(sep, maxsplit)]

    class AccountStream(io.StringIO):
        def read(self, size: int = -1) -> str:
            raise AssertionError("account lookup must stream records")

        def readline(self, size: int | None = -1) -> str:
            assert size is not None and 0 < size <= 1024 * 1024 + 1
            return AccountLine(super().readline(size))

    user_record = "root:x:0:0::/root:/bin/sh\n"
    group_record = "tools:x:2:root\n"
    if shape == "newlines":
        passwd = "\n" * (1024 * 1024 - len(user_record)) + user_record
        groups = "\n" * (1024 * 1024 - len(group_record)) + group_record
    elif shape == "fields":
        passwd = ":" * (1024 * 1024)
        groups = passwd
    elif shape == "members":
        passwd = user_record
        groups = "tools:x:2:" + "," * (1024 * 1024 - 15) + "root\n"
    else:
        passwd = user_record + "\n" * (1024 * 1024)
        groups = ""
    streams = {20: AccountStream(passwd), 21: AccountStream(groups)}
    closed: list[int] = []
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
    monkeypatch.setattr(
        worker_code,
        "os",
        SimpleNamespace(
            O_RDONLY=0,
            O_NOFOLLOW=1,
            O_NONBLOCK=2,
            stat=lambda *args, **kwargs: metadata,
            fstat=lambda fd: metadata,
            open=lambda path, flags: 20 if path == "/etc/passwd" else 21,
            fdopen=lambda fd, *args, **kwargs: streams[fd],
            close=closed.append,
        ),
    )
    if shape == "oversized":
        with pytest.raises(ValueError, match="account_file_too_large"):
            worker_code._user_ids("0")
        assert closed == [20]
    else:
        identity = worker_code._user_ids("0")
        assert identity == (0, 0, [] if shape == "fields" else [2])
        assert closed == [20, 21]
        assert streams[21].closed
    assert streams[20].closed


def test_account_stream_closes_on_group_conversion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    def accounts(path: str) -> Iterator[list[str]]:
        try:
            if path == "/etc/passwd":
                yield ["developer", "x", "1000", "1000", "", "/home/developer", "/bin/sh"]
            else:
                yield ["tools", "x", "invalid", "developer"]
                pytest.fail("lookup must stop after conversion failure")
        finally:
            closed.append(path)

    monkeypatch.setattr(worker_code, "_accounts", accounts)
    with pytest.raises(ValueError):
        worker_code._user_ids("developer")
    assert closed == ["/etc/passwd", "/etc/group"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
async def test_workspace_overridden_grant_does_not_invalidate_later_removal(
    service: Any, restore: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/workspace/cache", read_only=True),
            SandboxPathGrant(path="/external/protected", read_only=True),
        ),
    )
    manager.bind_new(container, configured)
    current = session(manager, container, configured)
    bindings = object.__new__(worker_code._Bindings)
    bindings.paths = ["/workspace", "/workspace/cache", "/external/protected"]
    bindings.fds = [20, 21, 22]
    originals = {
        path: SimpleNamespace(st_dev=1, st_ino=index)
        for index, path in enumerate([*bindings.paths, "/workspace/build"])
    }
    remaining = dict(originals)
    pinned = dict(zip(bindings.fds, originals.values(), strict=False))

    def metadata(path: str, **kwargs: Any) -> SimpleNamespace:
        if path not in remaining:
            raise FileNotFoundError(path)
        return remaining[path]

    monkeypatch.setattr(worker_code.os, "stat", metadata)
    monkeypatch.setattr(worker_code.os, "fstat", pinned.__getitem__)
    monkeypatch.setattr(worker_code, "_canonical", lambda path: path)
    original_request = worker.request

    def request(**data: Any) -> dict[str, Any]:
        if data["operation"] == "inspect":
            try:
                bindings.validate()
            except Exception as exc:
                raise RuntimeError(type(exc).__name__) from None
        result = original_request(**data)
        if data["operation"] == "remove":
            remaining.pop(worker.selected)
        return result

    monkeypatch.setattr(worker, "request", request)
    if restore:

        async def listing(_: Path) -> list[FileEntry]:
            return [
                FileEntry(
                    path=path,
                    kind=EntryKind.DIRECTORY,
                    permissions=Permissions(directory=True),
                    owner="0",
                    group="0",
                    size=0,
                )
                for path in ("/workspace/cache", "/workspace/build")
            ]

        monkeypatch.setattr(current, "ls", listing)
        await current._clear_workspace_dir_on_resume_pruned(
            current_dir=Path("/workspace"), skip_rel_paths=set()
        )
    else:
        await current.rm("cache", recursive=True)
        await current.rm("build", recursive=True)
    assert worker.removed == ["/workspace/cache", "/workspace/build"]
    assert set(remaining) == {"/workspace", "/external/protected"}

    # Effective external protection still fails closed if its identity changes.
    remaining["/external/protected"] = SimpleNamespace(st_dev=1, st_ino=100)
    with pytest.raises(WorkspaceArchiveWriteError):
        await current.rm("another-build", recursive=True)
    assert worker.removed == ["/workspace/cache", "/workspace/build"]


@pytest.mark.parametrize("failure", ["open", "register", "fstat", "none"])
def test_bound_descriptors_are_closed_after_partial_or_normal_lifetime(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    metadata = SimpleNamespace(st_dev=1, st_ino=1, st_mode=stat.S_IFDIR | 0o755)
    monkeypatch.setattr(worker_code.os, "O_PATH", 0, raising=False)
    monkeypatch.setattr(worker_code, "_canonical", lambda path: path)
    monkeypatch.setattr(worker_code.os, "stat", lambda path: metadata)
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    if failure == "register":
        callback = worker_code.ExitStack.callback

        def register(stack: Any, close: Any, fd: int) -> Any:
            if fd == 21:
                raise OSError("registration failed")
            return callback(stack, close, fd)

        monkeypatch.setattr(worker_code.ExitStack, "callback", register)
    monkeypatch.setattr(
        worker_code.os,
        "open",
        Mock(side_effect=[20, OSError("open failed") if failure == "open" else 21]),
    )
    monkeypatch.setattr(
        worker_code.os,
        "fstat",
        Mock(
            side_effect=[
                metadata,
                OSError("fstat failed") if failure == "fstat" else metadata,
                metadata,
            ]
        ),
    )
    if failure == "none":
        with worker_code._bind_paths(["/workspace", "/protected"]) as bindings:
            assert bindings.paths == ["/workspace", "/protected"]
            assert closed == []
    else:
        with pytest.raises(OSError):
            with worker_code._bind_paths(["/workspace", "/protected"]):
                pytest.fail("binding must fail")
    assert closed == ([20] if failure == "open" else [21, 20])


@pytest.mark.parametrize(
    ("failure", "limit_failure"),
    [
        (None, None),
        (PermissionError(13, "denied"), None),
        (OSError(errno.E2BIG, "removal_entry_limit"), None),
        (ValueError("unknown_user"), None),
        (KeyboardInterrupt(), None),
        (None, PermissionError(1, "limit denied")),
    ],
)
def test_removal_child_always_exits_without_resuming_parent(
    failure: BaseException | None, limit_failure: OSError | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ChildExited(BaseException):
        pass

    writes: list[dict[str, Any]] = []
    exit_codes: list[int] = []

    def exit_child(code: int) -> None:
        exit_codes.append(code)
        raise ChildExited

    user_ids = Mock(
        return_value=(1000, 1000, []),
        side_effect=failure if isinstance(failure, ValueError) else None,
    )
    monkeypatch.setattr(worker_code, "_user_ids", user_ids)
    monkeypatch.setattr(worker_code.os, "pipe", lambda: (20, 21))
    monkeypatch.setattr(worker_code.os, "fork", lambda: 0)
    set_limit = Mock(side_effect=limit_failure)
    monkeypatch.setattr(worker_code.resource, "setrlimit", set_limit)
    monkeypatch.setattr(worker_code.os, "close", lambda fd: None)
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(worker_code.os, name, lambda value: None)
    monkeypatch.setattr(worker_code, "_remove", Mock(side_effect=failure))
    monkeypatch.setattr(worker_code.os, "write", lambda fd, data: writes.append(json.loads(data)))
    monkeypatch.setattr(worker_code.os, "_exit", exit_child)
    with pytest.raises(ChildExited):
        worker_code._remove_as_user(
            "/workspace/build", "1000:1000", max_entry_visits=100_000, max_cpu_seconds=10
        )
    set_limit.assert_called_once_with(worker_code.resource.RLIMIT_CPU, (10, 10))
    if limit_failure is not None:
        user_ids.assert_not_called()
    failure = limit_failure or failure
    assert exit_codes == ([1] if isinstance(failure, KeyboardInterrupt) else [0])
    assert writes == (
        []
        if isinstance(failure, KeyboardInterrupt)
        else [
            {
                "ok": False,
                "reason": type(failure).__name__,
                "errno": getattr(failure, "errno", None),
            }
        ]
        if failure
        else [{"ok": True}]
    )


def test_worker_closes_bindings_when_response_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    close = Mock()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(
        worker_code.sys, "stdin", io.StringIO('{"operation":"bind","paths":["/workspace"]}\n')
    )
    monkeypatch.setattr(worker_code.sys, "stdout", Mock(write=Mock(side_effect=BrokenPipeError)))
    monkeypatch.setattr(worker_code, "_enter_container", lambda pid: None)

    @contextmanager
    def bind_paths(paths: list[str]) -> Iterator[Any]:
        try:
            yield SimpleNamespace(paths=paths)
        finally:
            close()

    monkeypatch.setattr(worker_code, "_bind_paths", bind_paths)
    with pytest.raises(BrokenPipeError):
        worker_code.main()
    close.assert_called_once_with()


def test_namespace_entry_closes_mount_handle_when_root_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    enter = Mock()
    monkeypatch.setattr(
        worker_code.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(setns=enter)
    )
    monkeypatch.setattr(
        worker_code.os, "open", Mock(side_effect=[20, PermissionError("root denied")])
    )
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    with pytest.raises(PermissionError):
        worker_code._enter_container(123)
    assert closed == [20]
    enter.assert_not_called()


class _DeepRemovalTree:
    """Model kernel path limits and descriptor ownership without creating a real tree."""

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self.remaining = set(range(depth + 1))
        self.removed: list[int] = []
        self.fds: dict[int, int] = {}
        self.next_fd = 100
        self.open_scans = 0
        self.peak_fds = 0

    def lookup(self, path: str, dir_fd: int | None) -> int:
        if len(path) >= 4096:
            raise OSError(errno.ENAMETOOLONG, "path too long")
        if dir_fd is None:
            assert path == "/tree"
            node = 0
        else:
            assert path in ("d", ".."), "descendants must use a single relative component"
            node = self.fds[dir_fd] + (1 if path == "d" else -1)
        if node not in self.remaining:
            raise FileNotFoundError(path)
        return node

    def lstat(self, path: str, *, dir_fd: int | None = None) -> SimpleNamespace:
        self.lookup(path, dir_fd)
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def rmdir(self, path: str, *, dir_fd: int | None = None) -> None:
        node = self.lookup(path, dir_fd)
        if node + 1 in self.remaining:
            raise OSError(errno.ENOTEMPTY, "not empty")
        self.remaining.remove(node)
        self.removed.append(node)

    def open(self, path: str, flags: int, *, dir_fd: int | None = None) -> int:
        assert flags & worker_code.os.O_DIRECTORY
        assert flags & worker_code.os.O_NOFOLLOW
        assert self.open_scans == 0
        node = self.lookup(path, dir_fd)
        self.next_fd += 1
        self.fds[self.next_fd] = node
        self.peak_fds = max(self.peak_fds, len(self.fds))
        assert self.peak_fds <= 2
        return self.next_fd

    def close(self, fd: int) -> None:
        del self.fds[fd]

    @contextmanager
    def scandir(self, fd: int) -> Iterator[Any]:
        node = self.fds[fd]
        self.open_scans += 1
        assert self.open_scans == 1
        try:
            yield iter([SimpleNamespace(name="d")] if node + 1 in self.remaining else [])
        finally:
            self.open_scans -= 1

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("lstat", "rmdir", "open", "close", "scandir"):
            monkeypatch.setattr(worker_code.os, name, getattr(self, name))
        monkeypatch.setattr(
            worker_code.os, "unlink", Mock(side_effect=AssertionError("directories only"))
        )


def test_worker_removes_deep_tree_beyond_path_max_with_bounded_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = _DeepRemovalTree(2500)
    assert len("/tree" + "/d" * tree.depth) > 4096
    tree.install(monkeypatch)
    worker_code._remove("/tree", max_entry_visits=100_000)
    assert tree.remaining == set()
    assert tree.removed == list(range(tree.depth, -1, -1))
    assert tree.fds == {}
    assert tree.open_scans == 0
    assert tree.peak_fds == 2


@pytest.mark.parametrize("failure", ["child_open", "parent_open", "scan", "remove", "budget"])
def test_worker_closes_traversal_descriptors_after_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    tree = _DeepRemovalTree(3)
    tree.install(monkeypatch)
    error = PermissionError(errno.EACCES, "denied")
    if failure in ("child_open", "parent_open"):

        def open_directory(path: str, flags: int, *, dir_fd: int | None = None) -> int:
            if path == ("d" if failure == "child_open" else ".."):
                raise error
            return tree.open(path, flags, dir_fd=dir_fd)

        monkeypatch.setattr(worker_code.os, "open", open_directory)
    elif failure == "scan":

        @contextmanager
        def scan(fd: int) -> Iterator[Any]:
            with tree.scandir(fd):
                yield iter(Mock(side_effect=error), None)

        monkeypatch.setattr(worker_code.os, "scandir", scan)
    elif failure == "remove":

        def rmdir(path: str, *, dir_fd: int | None = None) -> None:
            if tree.lookup(path, dir_fd) == tree.depth:
                raise error
            tree.rmdir(path, dir_fd=dir_fd)

        monkeypatch.setattr(worker_code.os, "rmdir", rmdir)
    with pytest.raises(OSError) as caught:
        worker_code._remove("/tree", max_entry_visits=2 if failure == "budget" else 100_000)
    assert caught.value.errno == (errno.E2BIG if failure == "budget" else errno.EACCES)
    assert tree.fds == {}
    assert tree.open_scans == 0


@pytest.mark.parametrize("leaf_mode", [stat.S_IFREG, stat.S_IFLNK])
def test_worker_streams_wide_directory_without_buffering_sibling_paths(
    monkeypatch: pytest.MonkeyPatch,
    leaf_mode: int,
) -> None:
    count = 10000
    deleted = 0
    root_removed = False

    def lstat(path: str, *, dir_fd: int | None = None) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFDIR if path == "/tree" else leaf_mode)

    def rmdir(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal root_removed
        if deleted != count:
            raise OSError(errno.ENOTEMPTY, "not empty")
        root_removed = True

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal deleted
        assert dir_fd == 123
        assert path == str(deleted)
        deleted += 1

    def entries() -> Iterator[SimpleNamespace]:
        for index in range(count):
            assert deleted == index, "each leaf must be consumed before fetching the next"
            yield SimpleNamespace(name=str(index))

    monkeypatch.setattr(worker_code.os, "lstat", lstat)
    monkeypatch.setattr(worker_code.os, "rmdir", rmdir)
    monkeypatch.setattr(worker_code.os, "unlink", unlink)
    monkeypatch.setattr(worker_code.os, "open", lambda *args, **kwargs: 123)
    closed: list[int] = []
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    monkeypatch.setattr(worker_code.os, "scandir", lambda fd: nullcontext(entries()))
    worker_code._remove("/tree", max_entry_visits=100_000)
    assert root_removed
    assert deleted == count
    assert closed == [123]


def test_worker_stops_entry_visits_before_additional_filesystem_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited: list[str] = []
    scans_closed: list[bool] = []

    def remove_leaf(path: str, *, dir_fd: int | None = None) -> bool:
        visited.append(path)
        return path != "/tree"

    @contextmanager
    def scan(path: str) -> Iterator[Any]:
        try:
            yield (SimpleNamespace(name=str(index)) for index in range(100))
        finally:
            scans_closed.append(True)

    monkeypatch.setattr(worker_code, "_remove_leaf", remove_leaf)
    monkeypatch.setattr(worker_code.os, "open", lambda *args, **kwargs: 123)
    closed: list[int] = []
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    monkeypatch.setattr(worker_code.os, "scandir", scan)
    with pytest.raises(OSError) as caught:
        worker_code._remove("/tree", max_entry_visits=3)
    assert caught.value.errno == errno.E2BIG
    assert visited == ["/tree", "0", "1"]
    assert closed == [123]
    assert scans_closed == [True]


@pytest.mark.parametrize("outcome", ["allowed", "search_denied", "repointed"])
def test_worker_preserves_workspace_alias_permissions_and_bound_identity(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = "/private/workspace-alias"
    original = alias + "/build"
    requests = [
        {"operation": "bind", "paths": [alias]},
        {"operation": "inspect", "path": original, "workspace_root": alias},
        {
            "operation": "remove",
            "user": "developer",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    output = io.StringIO()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests))))
    monkeypatch.setattr(worker_code.sys, "stdout", output)
    monkeypatch.setattr(worker_code, "_enter_container", lambda pid: None)
    monkeypatch.setattr(
        worker_code,
        "_bind_paths",
        lambda paths: nullcontext(SimpleNamespace(paths=["/canonical"], validate=lambda: None)),
    )
    monkeypatch.setattr(
        worker_code,
        "_canonical",
        lambda path: "/different" if outcome == "repointed" else "/canonical",
    )
    current_user = "root"
    removed: list[str] = []

    def metadata(path: str, *, dir_fd: int | None = None) -> SimpleNamespace:
        if current_user == "developer" and path.startswith(alias) and outcome == "search_denied":
            raise PermissionError("workspace alias ancestor denies search")
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def remove_as_user(path: str, user: str, **limits: int) -> dict[str, Any]:
        nonlocal current_user
        current_user = user
        worker_code._remove(path, max_entry_visits=100_000)
        return {"ok": True}

    monkeypatch.setattr(worker_code.os, "lstat", metadata)
    monkeypatch.setattr(worker_code.os, "rmdir", lambda path, **kwargs: removed.append(path))
    monkeypatch.setattr(worker_code, "_remove_as_user", remove_as_user)
    worker_code.main()
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    if outcome == "repointed":
        assert responses[1] == {"ok": False, "reason": "ValueError", "errno": None}
        assert responses[2] == {"ok": False, "reason": "ValueError", "errno": None}
    elif outcome == "search_denied":
        assert responses[1]["path"] == "/canonical/build"
        assert responses[2] == {"ok": False, "reason": "PermissionError", "errno": None}
    else:
        assert responses[1]["path"] == "/canonical/build"
        assert responses[2] == {"ok": True}
    assert removed == ([original] if outcome == "allowed" else [])
