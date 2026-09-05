from __future__ import annotations

import asyncio
import io
import signal
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents.sandbox import SandboxPathGrant
from agents.sandbox.errors import (
    InvalidManifestPathError,
    PtySessionNotFoundError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.sandboxes import unix_local as unix_local_module
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
    _UnixPtyProcessEntry,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, User


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


class TestUnixLocalRmSymlinks:
    @pytest.mark.asyncio
    async def test_rm_removes_file_symlink_not_its_target(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "plain.txt"
        target.write_text("keep", encoding="utf-8")
        link = workspace / "linkfile"
        link.symlink_to("plain.txt")
        session = _RecordingUnixLocalSession(workspace)

        await session.rm("linkfile")

        assert not link.is_symlink()
        assert target.read_text(encoding="utf-8") == "keep"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recursive", [False, True])
    async def test_rm_removes_directory_symlink_not_its_target(
        self,
        tmp_path: Path,
        recursive: bool,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "realdir" / "inner"
        target_dir.mkdir(parents=True)
        data = target_dir / "data.txt"
        data.write_text("keep", encoding="utf-8")
        link = workspace / "linkdir"
        link.symlink_to("realdir")
        session = _RecordingUnixLocalSession(workspace)

        await session.rm("linkdir", recursive=recursive)

        assert not link.is_symlink()
        assert data.read_text(encoding="utf-8") == "keep"

    @pytest.mark.asyncio
    async def test_rm_removes_symlink_pointing_outside_the_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = workspace / "escape"
        link.symlink_to(outside)
        session = _RecordingUnixLocalSession(workspace)

        await session.rm("escape")

        assert not link.is_symlink()
        assert outside.read_text(encoding="utf-8") == "secret"

    @pytest.mark.asyncio
    async def test_rm_removes_dangling_symlink(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        link = workspace / "dangling"
        link.symlink_to(tmp_path / "missing")
        session = _RecordingUnixLocalSession(workspace)

        await session.rm("dangling")

        assert not link.is_symlink()

    @pytest.mark.asyncio
    async def test_rm_still_rejects_entries_reached_through_an_escaping_symlink(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        victim = outside_dir / "victim.txt"
        victim.write_text("secret", encoding="utf-8")
        (workspace / "escape_dir").symlink_to(outside_dir)
        session = _RecordingUnixLocalSession(workspace)

        with pytest.raises(InvalidManifestPathError):
            await session.rm("escape_dir/victim.txt")

        assert victim.read_text(encoding="utf-8") == "secret"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_path", ["C:\\outside\\file", "/outside/file", "../file"])
    async def test_rm_rejects_absolute_and_escaping_raw_paths_before_splitting_the_leaf(
        self,
        tmp_path: Path,
        raw_path: str,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # A workspace entry literally named like the raw path must not be what gets removed.
        decoy = workspace / raw_path.split("/")[-1]
        decoy.write_text("keep", encoding="utf-8")
        session = _RecordingUnixLocalSession(workspace)

        with pytest.raises(InvalidManifestPathError):
            await session.rm(raw_path)

        assert decoy.read_text(encoding="utf-8") == "keep"

    @pytest.mark.asyncio
    async def test_rm_applies_the_most_specific_grant_to_the_leaf(self, tmp_path: Path) -> None:
        """A link into a writable grant must not let rm delete a nested read-only grant root."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        shared = tmp_path / "shared"
        protected = shared / "protected"
        protected.mkdir(parents=True)
        (protected / "keep.txt").write_text("keep", encoding="utf-8")
        (workspace / "tmp-link").symlink_to(shared)
        session = UnixLocalSandboxSession(
            state=UnixLocalSandboxSessionState(
                manifest=Manifest(
                    root=str(workspace),
                    extra_path_grants=(
                        SandboxPathGrant(path=str(shared)),
                        SandboxPathGrant(path=str(protected), read_only=True),
                    ),
                ),
                snapshot=NoopSnapshot(id="noop"),
            )
        )

        with pytest.raises(WorkspaceArchiveWriteError):
            await session.rm("tmp-link/protected", recursive=True)

        assert (protected / "keep.txt").read_text(encoding="utf-8") == "keep"
        (shared / "scratch.txt").write_text("scratch", encoding="utf-8")
        await session.rm("tmp-link/scratch.txt")
        assert not (shared / "scratch.txt").exists()

    @pytest.mark.asyncio
    async def test_rm_accepts_paths_resolved_through_a_symlinked_root(
        self,
        tmp_path: Path,
    ) -> None:
        """Manifest.root may be a symlink; ls() reports resolved paths that rm() must accept."""
        real_root = tmp_path / "ws"
        real_root.mkdir()
        root_link = tmp_path / "ws-link"
        root_link.symlink_to(real_root)
        (real_root / "via-real.txt").write_text("x", encoding="utf-8")
        (real_root / "via-link.txt").write_text("x", encoding="utf-8")
        session = _RecordingUnixLocalSession(root_link)

        await session.rm(str(real_root / "via-real.txt"))
        await session.rm(str(root_link / "via-link.txt"))

        assert not (real_root / "via-real.txt").exists()
        assert not (real_root / "via-link.txt").exists()

    @pytest.mark.asyncio
    async def test_rm_validates_the_symlinked_root_alias_itself(self, tmp_path: Path) -> None:
        """Naming the root through its alias must not be misread as removing the alias link."""
        real_root = tmp_path / "ws"
        real_root.mkdir()
        root_link = tmp_path / "ws-link"
        root_link.symlink_to(real_root)
        (tmp_path / "dummy").mkdir()
        # A noncanonical spelling of the root alias must be recognized as the root too.
        session = _RecordingUnixLocalSession(tmp_path / "dummy" / ".." / "ws-link")

        assert session._rm_target_path(".") == real_root
        assert session._rm_target_path(str(root_link)) == real_root
        # The configured (noncanonical) spelling itself must also name the root.
        assert session._rm_target_path(str(tmp_path / "dummy" / ".." / "ws-link")) == real_root

    @pytest.mark.asyncio
    async def test_rm_as_user_checks_the_symlink_entry_and_keeps_its_target(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "plain.txt"
        target.write_text("keep", encoding="utf-8")
        link = workspace / "linkfile"
        link.symlink_to("plain.txt")
        session = _RecordingUnixLocalSession(workspace)

        await session.rm("linkfile", user=User(name="sandbox-user"))

        assert not link.is_symlink()
        assert target.read_text(encoding="utf-8") == "keep"
        assert len(session.exec_commands) == 1
        assert session.exec_commands[0][-2:] == (str(link), "0")


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
