from __future__ import annotations

import asyncio
import io
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents.sandbox import SandboxPathGrant
from agents.sandbox.errors import PtySessionNotFoundError, WorkspaceArchiveReadError
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


@pytest.mark.asyncio
async def test_persist_workspace_archives_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archiving the workspace must not hold the loop for the length of the tar.

    The stub returns only after a coroutine scheduled alongside it has run, so archiving on
    the event loop deadlocks here instead of completing.
    """
    session = _RecordingUnixLocalSession(tmp_path)
    released = threading.Event()
    archived: list[tuple[Path, set[Path]]] = []

    def blocking_archive(root: Path, skip: set[Path]) -> io.BytesIO:
        if not released.wait(timeout=5):
            raise AssertionError("the event loop was blocked while archiving the workspace")
        archived.append((root, skip))
        return io.BytesIO(b"archive")

    monkeypatch.setattr(unix_local_module, "_archive_workspace", blocking_archive)

    async def release() -> None:
        released.set()

    archive, _ = await asyncio.gather(session.persist_workspace(), release())

    assert archived == [(Path(tmp_path), session._persist_workspace_skip_relpaths())]
    assert archive.read() == b"archive"


@pytest.mark.asyncio
async def test_hydrate_workspace_extracts_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction is unbounded too, so it runs on a worker thread for the same reason."""
    session = _RecordingUnixLocalSession(tmp_path)
    released = threading.Event()
    extracted: list[Path] = []

    def blocking_extract(root: Path, data: io.IOBase) -> None:
        if not released.wait(timeout=5):
            raise AssertionError("the event loop was blocked while extracting the archive")
        extracted.append(root)

    monkeypatch.setattr(unix_local_module, "_extract_workspace_archive", blocking_extract)

    async def release() -> None:
        released.set()

    await asyncio.gather(session.hydrate_workspace(io.BytesIO(b"")), release())

    assert extracted == [Path(tmp_path)]


@pytest.mark.asyncio
async def test_persist_then_hydrate_round_trips_workspace_contents(tmp_path: Path) -> None:
    """Moving the tar work into helpers must not change what the archive carries."""
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file.txt").write_text("payload", encoding="utf-8")

    archive = await _RecordingUnixLocalSession(source).persist_workspace()

    target = tmp_path / "target"
    await _RecordingUnixLocalSession(target).hydrate_workspace(archive)

    assert (target / "nested" / "file.txt").read_text(encoding="utf-8") == "payload"


@pytest.mark.asyncio
async def test_persist_workspace_reports_a_missing_root(tmp_path: Path) -> None:
    session = _RecordingUnixLocalSession(tmp_path / "absent")

    with pytest.raises(WorkspaceArchiveReadError):
        await session.persist_workspace()
