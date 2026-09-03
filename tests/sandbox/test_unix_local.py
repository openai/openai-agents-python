from __future__ import annotations

import asyncio
import io
import signal
import socket
import stat
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents.sandbox import SandboxPathGrant
from agents.sandbox.errors import (
    ExecNonZeroError,
    InvalidManifestPathError,
    PtySessionNotFoundError,
)
from agents.sandbox.files import EntryKind
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.sandboxes import unix_local as unix_local_module
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
    _UnixPtyProcessEntry,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, Permissions, User


class _RecordingUnixLocalSession(UnixLocalSandboxSession):
    def __init__(self, root: Path) -> None:
        super().__init__(
            state=UnixLocalSandboxSessionState(
                manifest=Manifest(root=str(root)),
                snapshot=NoopSnapshot(id="noop"),
            )
        )
        self.exec_commands: list[tuple[str, ...]] = []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        self.exec_commands.append(tuple(str(part) for part in command))
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)


@pytest.mark.asyncio
async def test_unix_local_inherits_host_environment_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unix_local_module.sys, "platform", "linux")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    monkeypatch.setenv("LC_MESSAGES", "C")
    monkeypatch.setenv("LC_PRIVATE_TOKEN", "locale-secret")
    workspace = tmp_path / "workspace"
    manifest = Manifest(
        root=str(workspace),
        environment=Environment(
            value={
                "HOME": "/manifest-home",
                "LC_CTYPE": "POSIX",
                "MANIFEST_ONLY": "configured",
            }
        ),
    )

    async with await UnixLocalSandboxClient().create(
        manifest=manifest, snapshot=None, options=None
    ) as session:
        result = await session.exec(
            "sh",
            "-c",
            "printf '%s|%s|%s|%s|%s|%s|%s' "
            '"${OPENAI_API_KEY-unset}" "$MANIFEST_ONLY" "$HOME" '
            '"${PATH:+set}" "$LC_MESSAGES" "$LC_CTYPE" '
            '"${LC_PRIVATE_TOKEN-unset}"',
            shell=False,
        )

    assert result.exit_code == 0
    assert result.stdout.decode() == (
        f"host-secret|configured|{workspace}|set|C|POSIX|locale-secret"
    )


@pytest.mark.asyncio
async def test_unix_local_uses_default_allowlist_when_inheritance_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unix_local_module.sys, "platform", "linux")
    monkeypatch.setenv("HOST_ONLY_VALUE", "host-value")
    monkeypatch.setenv("LC_MESSAGES", "C")
    monkeypatch.setenv("LC_PRIVATE_TOKEN", "locale-secret")
    manifest = Manifest(root=str(tmp_path / "workspace"))
    isolated_client = UnixLocalSandboxClient(inherit_host_environment=False)

    async with await isolated_client.create(
        manifest=manifest, snapshot=None, options=None
    ) as session:
        created = await session.exec(
            "sh",
            "-c",
            "printf '%s|%s|%s' "
            '"${HOST_ONLY_VALUE-unset}" "$LC_MESSAGES" '
            '"${LC_PRIVATE_TOKEN-unset}"',
            shell=False,
        )
        state = session.state

    payload = isolated_client.serialize_session_state(state)
    assert "inherit_host_environment" not in payload
    assert "host_environment_allowlist" not in payload
    assert created.stdout == b"unset|C|unset"

    async with await isolated_client.resume(state) as resumed:
        isolated_after_resume = await resumed.exec(
            "sh", "-c", 'printf "%s" "${HOST_ONLY_VALUE-unset}"', shell=False
        )
    assert isolated_after_resume.stdout == b"unset"

    async with await UnixLocalSandboxClient().resume(state) as resumed_with_default:
        inherited_after_resume = await resumed_with_default.exec(
            "sh", "-c", 'printf "%s" "${HOST_ONLY_VALUE-unset}"', shell=False
        )
    assert inherited_after_resume.stdout == b"host-value"


@pytest.mark.asyncio
async def test_unix_local_uses_custom_host_environment_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(unix_local_module.sys, "platform", "linux")
    monkeypatch.setenv("CUSTOM_ALLOWED", "allowed-value")
    monkeypatch.setenv("HOST_ONLY_VALUE", "host-value")
    manifest = Manifest(root=str(tmp_path / "workspace"))
    client = UnixLocalSandboxClient(
        inherit_host_environment=False,
        host_environment_allowlist={"PATH", "CUSTOM_ALLOWED"},
    )

    async with await client.create(manifest=manifest, snapshot=None, options=None) as session:
        result = await session.exec(
            "sh",
            "-c",
            'printf \'%s|%s\' "$CUSTOM_ALLOWED" "${HOST_ONLY_VALUE-unset}"',
            shell=False,
        )
        state = session.state

    assert result.stdout == b"allowed-value|unset"

    async with await client.resume(state) as resumed:
        resumed_result = await resumed.exec(
            "sh",
            "-c",
            'printf \'%s|%s\' "$CUSTOM_ALLOWED" "${HOST_ONLY_VALUE-unset}"',
            shell=False,
        )

    assert resumed_result.stdout == b"allowed-value|unset"


def test_unix_local_rejects_invalid_host_environment_allowlist_configuration() -> None:
    with pytest.raises(
        ValueError,
        match="host_environment_allowlist requires inherit_host_environment=False",
    ):
        UnixLocalSandboxClient(host_environment_allowlist={"PATH"})

    with pytest.raises(
        TypeError,
        match="host_environment_allowlist must be a collection of variable names",
    ):
        UnixLocalSandboxClient(
            inherit_host_environment=False,
            host_environment_allowlist="PATH",
        )


@pytest.mark.asyncio
async def test_unix_local_rejects_host_path_before_creating_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_mkdtemp(*args: object, **kwargs: object) -> str:
        raise AssertionError(f"unexpected mkdtemp call: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        "agents.sandbox.sandboxes.unix_local.tempfile.mkdtemp",
        _unexpected_mkdtemp,
    )
    client = UnixLocalSandboxClient()

    with pytest.raises(
        ValueError,
        match="UnixLocalSandboxClient does not support sandbox path grant host_path",
    ):
        await client.create(
            manifest=Manifest(
                extra_path_grants=(
                    SandboxPathGrant(
                        path="/mnt/shared-data",
                        host_path=str(tmp_path),
                    ),
                )
            ),
            snapshot=None,
            options=None,
        )


@pytest.mark.review_optional
class TestUnixLocalPty:
    @pytest.mark.asyncio
    async def test_tty_fd_close_is_owned_without_blocking_termination(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _RecordingUnixLocalSession(tmp_path)
        close_started = asyncio.Event()
        release_close = asyncio.Event()

        async def blocked_to_thread(*args: object, **kwargs: object) -> None:
            _ = (args, kwargs)
            close_started.set()
            await release_close.wait()

        monkeypatch.setattr(asyncio, "to_thread", blocked_to_thread)
        process = cast(
            asyncio.subprocess.Process,
            SimpleNamespace(returncode=0, pid=None),
        )
        entry = _UnixPtyProcessEntry(process=process, tty=True, primary_fd=123)

        await asyncio.wait_for(session._terminate_pty_entry(entry), timeout=0.5)
        await close_started.wait()

        assert len(session._fd_close_tasks) == 1
        await asyncio.wait_for(session._after_stop(), timeout=0.5)
        assert len(session._fd_close_tasks) == 1

        release_close.set()
        await asyncio.gather(*session._fd_close_tasks)
        await asyncio.sleep(0)

        assert session._fd_close_tasks == set()

    @pytest.mark.asyncio
    @pytest.mark.requires_native_macos_sandbox
    async def test_pty_exec_write_poll_and_unknown_session_errors(self, tmp_path: Path) -> None:
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))

        async with await client.create(manifest=manifest, snapshot=None, options=None) as session:
            started = await session.pty_exec_start(
                "sh",
                "-c",
                "IFS= read -r line; printf '%s\\n' \"$line\"",
                shell=False,
                tty=True,
                yield_time_s=0.05,
            )

            assert started.process_id is not None
            assert started.exit_code is None

            written = await session.pty_write_stdin(
                session_id=started.process_id,
                chars="hello from pty\n",
                yield_time_s=0.25,
            )
            assert written.process_id is None
            assert written.exit_code == 0
            assert "hello from pty" in written.output.decode("utf-8", errors="replace")

            with pytest.raises(PtySessionNotFoundError):
                await session.pty_write_stdin(session_id=started.process_id, chars="")

            with pytest.raises(PtySessionNotFoundError):
                await session.pty_write_stdin(session_id=999_999, chars="")

    @pytest.mark.asyncio
    @pytest.mark.requires_native_macos_sandbox
    async def test_pty_ctrl_c_interrupts_long_running_process(self, tmp_path: Path) -> None:
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))

        async with await client.create(manifest=manifest, snapshot=None, options=None) as session:
            started = await session.pty_exec_start(
                "sleep",
                "30",
                shell=False,
                tty=True,
                yield_time_s=0.05,
            )

            assert started.process_id is not None
            assert started.exit_code is None

            first_interrupt = await session.pty_write_stdin(
                session_id=started.process_id,
                chars="\x03",
                yield_time_s=0.25,
            )
            if first_interrupt.process_id is None:
                interrupted = first_interrupt
            else:
                interrupted = await session.pty_write_stdin(
                    session_id=started.process_id,
                    chars="",
                    yield_time_s=5.5,
                )

            assert interrupted.process_id is None
            assert interrupted.exit_code is not None

            with pytest.raises(PtySessionNotFoundError):
                await session.pty_write_stdin(session_id=started.process_id, chars="")

    @pytest.mark.parametrize(
        ("signum", "chars"),
        [
            pytest.param(signal.SIGINT, "\x03", id="sigint"),
            pytest.param(signal.SIGQUIT, "\x1c", id="sigquit"),
        ],
    )
    @pytest.mark.asyncio
    @pytest.mark.requires_native_macos_sandbox
    async def test_pty_terminal_signals_interrupt_even_if_parent_ignores_signal(
        self, tmp_path: Path, signum: signal.Signals, chars: str
    ) -> None:
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))
        previous_handler = signal.getsignal(signum)

        signal.signal(signum, signal.SIG_IGN)
        try:
            async with await client.create(
                manifest=manifest, snapshot=None, options=None
            ) as session:
                started = await session.pty_exec_start(
                    "sleep",
                    "30",
                    shell=False,
                    tty=True,
                    yield_time_s=0.05,
                )
                assert started.process_id is not None

                interrupted = await session.pty_write_stdin(
                    session_id=started.process_id,
                    chars=chars,
                    yield_time_s=5.5,
                )

                assert interrupted.process_id is None
                assert interrupted.exit_code == -signum
        finally:
            signal.signal(signum, previous_handler)

    @pytest.mark.asyncio
    @pytest.mark.requires_native_macos_sandbox
    async def test_non_tty_pty_session_rejects_stdin_and_can_still_be_polled(
        self, tmp_path: Path
    ) -> None:
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))

        async with await client.create(manifest=manifest, snapshot=None, options=None) as session:
            started = await session.pty_exec_start(
                "sh",
                "-c",
                "printf 'stdout\\n'; printf 'stderr\\n' >&2; sleep 1",
                shell=False,
                tty=False,
                yield_time_s=0.05,
            )

            assert started.process_id is not None
            assert started.exit_code is None
            started_text = started.output.decode("utf-8", errors="replace")
            assert "stdout" in started_text
            assert "stderr" in started_text

            with pytest.raises(RuntimeError, match="stdin is not available for this process"):
                await session.pty_write_stdin(session_id=started.process_id, chars="hello")

            finished = await session.pty_write_stdin(
                session_id=started.process_id,
                chars="",
                yield_time_s=5.5,
            )
            text = finished.output.decode("utf-8", errors="replace")
            assert finished.process_id is None
            assert finished.exit_code == 0
            assert text == ""

            with pytest.raises(PtySessionNotFoundError):
                await session.pty_write_stdin(session_id=started.process_id, chars="")

    @pytest.mark.asyncio
    @pytest.mark.requires_native_macos_sandbox
    async def test_stop_terminates_active_pty_sessions(self, tmp_path: Path) -> None:
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))

        session = await client.create(manifest=manifest, snapshot=None, options=None)
        await session.start()
        started = await session.pty_exec_start(
            "sh",
            "-c",
            "printf 'ready\\n'; sleep 30",
            shell=False,
            tty=True,
            yield_time_s=0.25,
        )

        assert started.process_id is not None
        assert "ready" in started.output.decode("utf-8", errors="replace")

        await session.stop()

        with pytest.raises(PtySessionNotFoundError):
            await session.pty_write_stdin(session_id=started.process_id, chars="")


class TestUnixLocalUserScopedFilesystem:
    @pytest.mark.asyncio
    async def test_mkdir_as_user_checks_permissions_then_uses_local_fs(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = _RecordingUnixLocalSession(workspace)

        await session.mkdir("nested", user=User(name="sandbox-user"))

        assert (workspace / "nested").is_dir()
        assert len(session.exec_commands) == 1
        assert session.exec_commands[0][:4] == ("sudo", "-u", "sandbox-user", "--")
        assert session.exec_commands[0][4:6] == ("sh", "-lc")
        assert session.exec_commands[0][-2:] == (str(workspace / "nested"), "0")
        assert not any(part.startswith("mkdir ") for part in session.exec_commands[0])

    @pytest.mark.asyncio
    async def test_rm_as_user_checks_permissions_then_uses_local_fs(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "stale.txt"
        target.write_text("stale", encoding="utf-8")
        session = _RecordingUnixLocalSession(workspace)

        await session.rm("stale.txt", user=User(name="sandbox-user"))

        assert not target.exists()
        assert len(session.exec_commands) == 1
        assert session.exec_commands[0][:4] == ("sudo", "-u", "sandbox-user", "--")
        assert session.exec_commands[0][4:6] == ("sh", "-lc")
        assert session.exec_commands[0][-2:] == (str(target), "0")
        assert not any(part.startswith("rm ") for part in session.exec_commands[0])


@pytest.mark.parametrize(
    ("mode", "directory"),
    [
        (stat.S_IFDIR | 0o755, True),
        (stat.S_IFREG | 0o644, False),
        (stat.S_IFLNK | 0o777, False),
        (stat.S_IFSOCK | 0o755, False),  # S_IFSOCK shares the S_IFDIR bit
        (stat.S_IFBLK | 0o660, False),  # so does S_IFBLK
    ],
)
def test_permissions_from_mode_uses_the_file_type_not_a_bit_mask(
    mode: int,
    directory: bool,
) -> None:
    assert Permissions.from_mode(mode).directory is directory


class TestUnixLocalLs:
    @pytest.mark.asyncio
    async def test_ls_of_a_regular_file_returns_that_entry(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "plain.txt").write_text("hello", encoding="utf-8")
        session = _RecordingUnixLocalSession(workspace)

        entries = await session.ls("plain.txt")

        assert [(entry.kind, entry.path, entry.size) for entry in entries] == [
            (EntryKind.FILE, str(workspace / "plain.txt"), len("hello"))
        ]

    @pytest.mark.asyncio
    async def test_ls_of_a_file_symlink_returns_the_link_itself(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "plain.txt").write_text("hello", encoding="utf-8")
        (workspace / "link").symlink_to("plain.txt")
        session = _RecordingUnixLocalSession(workspace)

        entries = await session.ls("link")

        assert [(entry.kind, entry.path) for entry in entries] == [
            (EntryKind.SYMLINK, str(workspace / "link"))
        ]

    @pytest.mark.asyncio
    async def test_ls_of_a_socket_is_not_reported_as_a_directory(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(workspace / "dev.sock"))
        finally:
            sock.close()
        session = _RecordingUnixLocalSession(workspace)

        entries = await session.ls("dev.sock")

        assert [(entry.kind, entry.permissions.directory) for entry in entries] == [
            (EntryKind.OTHER, False)
        ]

    @pytest.mark.asyncio
    async def test_ls_as_user_lists_the_link_itself(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "plain.txt").write_text("hello", encoding="utf-8")
        (workspace / "link").symlink_to("plain.txt")
        session = _RecordingUnixLocalSession(workspace)

        await session.ls("link", user=User(name="sandbox-user"))

        assert len(session.exec_commands) == 1
        assert session.exec_commands[0][:4] == ("sudo", "-u", "sandbox-user", "--")
        assert session.exec_commands[0][4:] == ("ls", "-la", "--", str(workspace / "link"))

    @pytest.mark.asyncio
    async def test_ls_of_a_file_covered_by_an_exact_grant(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        granted = tmp_path / "outside.txt"
        granted.write_text("granted", encoding="utf-8")
        session = UnixLocalSandboxSession(
            state=UnixLocalSandboxSessionState(
                manifest=Manifest(
                    root=str(workspace),
                    extra_path_grants=(SandboxPathGrant(path=str(granted), read_only=True),),
                ),
                snapshot=NoopSnapshot(id="noop"),
            )
        )

        entries = await session.ls(str(granted))

        assert [(entry.kind, entry.path) for entry in entries] == [(EntryKind.FILE, str(granted))]

    @pytest.mark.asyncio
    async def test_ls_of_a_directory_symlink_returns_the_link_itself(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "realdir").mkdir(parents=True)
        (workspace / "realdir" / "inner.txt").write_text("x", encoding="utf-8")
        (workspace / "linkdir").symlink_to("realdir")
        session = _RecordingUnixLocalSession(workspace)

        entries = await session.ls("linkdir")

        assert [(entry.kind, entry.path) for entry in entries] == [
            (EntryKind.SYMLINK, str(workspace / "linkdir"))
        ]
        assert {Path(entry.path).name for entry in await session.ls("realdir")} == {"inner.txt"}

    @pytest.mark.asyncio
    async def test_ls_never_rebuilds_the_leaf_through_a_symlinked_parent(
        self,
        tmp_path: Path,
    ) -> None:
        """`link/../secret` is validated as `<root>/secret`; the leaf must be rebuilt from that
        canonical path rather than by following `link` and stepping out of the workspace."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        (workspace / "link").symlink_to(outside)
        session = _RecordingUnixLocalSession(workspace)

        with pytest.raises(ExecNonZeroError):
            await session.ls("link/../secret")

        (workspace / "secret").write_text("inside", encoding="utf-8")
        entries = await session.ls("link/../secret")
        assert [(entry.kind, entry.path) for entry in entries] == [
            (EntryKind.FILE, str(workspace / "secret"))
        ]

    @pytest.mark.asyncio
    async def test_ls_rejects_a_leaf_that_lives_outside_the_workspace(self, tmp_path: Path) -> None:
        """`jump/back` resolves back inside the workspace, but the entry itself lives in an
        ungranted directory; its metadata must not be read."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "inside.txt").write_text("inside", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "back").symlink_to(workspace / "inside.txt")
        (workspace / "jump").symlink_to(outside)
        session = _RecordingUnixLocalSession(workspace)

        with pytest.raises(InvalidManifestPathError):
            await session.ls("jump/back")

    @pytest.mark.asyncio
    async def test_ls_treats_backslashes_as_separators_for_both_checks(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "sub").mkdir(parents=True)
        (workspace / "sub" / "file.txt").write_text("x", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        (workspace / "link").symlink_to(outside)
        session = _RecordingUnixLocalSession(workspace)

        entries = await session.ls("sub\\file.txt")
        assert [(entry.kind, entry.path) for entry in entries] == [
            (EntryKind.FILE, str(workspace / "sub" / "file.txt"))
        ]
        with pytest.raises(InvalidManifestPathError):
            await session.ls("link\\secret")

    @pytest.mark.asyncio
    async def test_ls_keeps_a_leaf_symlink_addressed_through_the_resolved_root(
        self,
        tmp_path: Path,
    ) -> None:
        real_root = tmp_path / "ws"
        real_root.mkdir()
        root_link = tmp_path / "ws-link"
        root_link.symlink_to(real_root)
        (real_root / "plain.txt").write_text("x", encoding="utf-8")
        (real_root / "link").symlink_to("plain.txt")
        session = _RecordingUnixLocalSession(root_link)

        entries = await session.ls(str(real_root / "link"))

        assert [(entry.kind, entry.path) for entry in entries] == [
            (EntryKind.SYMLINK, str(real_root / "link"))
        ]

    @pytest.mark.asyncio
    async def test_ls_of_a_directory_lists_entries_with_kinds(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "sub").mkdir(parents=True)
        (workspace / "plain.txt").write_text("hello", encoding="utf-8")
        (workspace / "link").symlink_to("plain.txt")
        session = _RecordingUnixLocalSession(workspace)

        entries = await session.ls(".")

        assert {Path(entry.path).name: entry.kind for entry in entries} == {
            "sub": EntryKind.DIRECTORY,
            "plain.txt": EntryKind.FILE,
            "link": EntryKind.SYMLINK,
        }

    @pytest.mark.asyncio
    async def test_ls_of_a_missing_path_still_fails(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = _RecordingUnixLocalSession(workspace)

        with pytest.raises(ExecNonZeroError):
            await session.ls("missing")


@pytest.mark.asyncio
async def test_hydrate_workspace_cancellation_waits_for_the_extracting_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled hydrate must not leave a worker writing into the workspace.

    `restore_snapshot_into_workspace_on_resume` closes the archive stream in a `finally` as
    soon as its await returns, so if cancellation propagated while the extractor was still
    running it would read a closed stream and write into a workspace resume then clears.
    """
    workspace = tmp_path / "workspace"
    session = _RecordingUnixLocalSession(workspace)

    started = threading.Event()
    events: list[str] = []

    def _slow_extract(tar: object, **kwargs: object) -> None:
        _ = tar, kwargs
        events.append("extract-start")
        started.set()
        time.sleep(0.2)
        events.append("extract-end")

    monkeypatch.setattr(unix_local_module, "safe_extract_tarfile", _slow_extract)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    buf.seek(0)

    task = asyncio.create_task(session.hydrate_workspace(buf))
    while not started.is_set():
        await asyncio.sleep(0.005)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker finished before the caller observed cancellation, so the archive stream and
    # the workspace root are only released once nothing is still writing to them.
    assert events == ["extract-start", "extract-end"]
    assert not buf.closed
