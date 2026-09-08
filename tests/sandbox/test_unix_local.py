from __future__ import annotations

import asyncio
import io
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents.editor import ApplyPatchOperation
from agents.sandbox import SandboxPathGrant
from agents.sandbox.errors import (
    ApplyPatchDiffError,
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
from agents.sandbox.session.base_sandbox_session import (
    _EXCLUSIVE_CREATE_EXISTS_CODE,
    _EXCLUSIVE_CREATE_SCRIPT,
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
    async def test_tty_start_cancellation_closes_open_file_descriptors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(unix_local_module.sys, "platform", "linux")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = _RecordingUnixLocalSession(workspace)
        close_calls: list[int] = []

        def openpty() -> tuple[int, int]:
            return 101, 102

        async def create_subprocess(*args: object, **kwargs: object) -> None:
            _ = (args, kwargs)
            raise asyncio.CancelledError()

        monkeypatch.setattr(unix_local_module.os, "openpty", openpty)
        monkeypatch.setattr(unix_local_module.os, "close", close_calls.append)
        monkeypatch.setattr(unix_local_module.asyncio, "create_subprocess_exec", create_subprocess)

        with pytest.raises(asyncio.CancelledError):
            await session.pty_exec_start("echo", "hello", shell=False, tty=True)

        assert close_calls == [101, 102]

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
    @pytest.mark.parametrize(
        ("prefix", "tail", "first_output", "final_output"),
        [
            (b"before close", b" terminal", b"before close", b" terminal"),
            (b"\xc3", b"\xa9", b"", "é".encode()),
        ],
    )
    async def test_pty_exit_waits_for_output_close_before_terminal_cleanup(
        self,
        tmp_path: Path,
        prefix: bytes,
        tail: bytes,
        first_output: bytes,
        final_output: bytes,
    ) -> None:
        session = _RecordingUnixLocalSession(tmp_path)
        process = cast(
            asyncio.subprocess.Process,
            SimpleNamespace(returncode=0, pid=None),
        )
        entry = _UnixPtyProcessEntry(process=process, tty=False)
        process_id = 1234
        session._pty_processes[process_id] = entry
        session._reserved_pty_process_ids.add(process_id)

        entry.output_chunks.append(prefix)
        output, token_count, output_closed = await session._collect_pty_output(
            entry=entry,
            yield_time_ms=0,
            max_output_tokens=None,
        )
        # The producer can close and queue a terminal tail after collection returns but
        # before finalization observes the entry. Removal must follow the collector's
        # settled result, not a later read of the mutable close event.
        entry.output_chunks.append(tail)
        entry.output_closed.set()
        still_live = await session._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=output,
            original_token_count=token_count,
            output_closed=output_closed,
        )

        assert still_live.process_id == process_id
        assert still_live.exit_code is None
        assert still_live.output == first_output
        assert process_id in session._pty_processes

        terminal_output, terminal_token_count, terminal_closed = await session._collect_pty_output(
            entry=entry,
            yield_time_ms=0,
            max_output_tokens=None,
        )
        terminal = await session._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=terminal_output,
            original_token_count=terminal_token_count,
            output_closed=terminal_closed,
        )

        assert terminal.process_id is None
        assert terminal.exit_code == 0
        assert terminal.output == final_output
        assert process_id not in session._pty_processes
        assert process_id not in session._reserved_pty_process_ids

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


def _exclusive_write_session(root: Path) -> UnixLocalSandboxSession:
    return UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(root)),
            snapshot=NoopSnapshot(id="noop"),
        )
    )


@pytest.mark.asyncio
async def test_write_new_file_keeps_an_intervening_creator_content(tmp_path: Path) -> None:
    """The name is claimed by the write itself, so a creator that got there first wins."""
    session = _exclusive_write_session(tmp_path)
    target = tmp_path / "notes.txt"
    target.write_bytes(b"written by someone else\n")

    with pytest.raises(FileExistsError):
        await session.write_new_file(Path("notes.txt"), io.BytesIO(b"clobbered"))

    assert target.read_bytes() == b"written by someone else\n"


@pytest.mark.asyncio
async def test_write_new_file_rejects_a_dangling_symlink(tmp_path: Path) -> None:
    """A symlink entry is not absent, and the write must not follow it to its target."""
    session = _exclusive_write_session(tmp_path)
    link = tmp_path / "link.txt"
    link.symlink_to(tmp_path / "missing.txt")

    with pytest.raises(FileExistsError):
        await session.write_new_file(Path("link.txt"), io.BytesIO(b"clobbered"))

    assert link.is_symlink()
    assert not (tmp_path / "missing.txt").exists()


@pytest.mark.asyncio
async def test_write_new_file_creates_a_file_and_its_parents(tmp_path: Path) -> None:
    session = _exclusive_write_session(tmp_path)

    await session.write_new_file(Path("nested/dir/new.txt"), io.BytesIO(b"payload"))

    assert (tmp_path / "nested" / "dir" / "new.txt").read_bytes() == b"payload"


class _ExitCodeUnixLocalSession(UnixLocalSandboxSession):
    """Drives the shared exec-based exclusive create with a chosen exit code."""

    def __init__(self, root: Path, exit_code: int, *, preflight_exit_code: int = 0) -> None:
        super().__init__(
            state=UnixLocalSandboxSessionState(
                manifest=Manifest(root=str(root)),
                snapshot=NoopSnapshot(id="noop"),
            )
        )
        self._exit_code = exit_code
        self._preflight_exit_code = preflight_exit_code
        self.exec_commands: list[tuple[str, ...]] = []
        self.writes: list[Path] = []
        self.removed: list[Path] = []
        self.made_dirs: list[Path] = []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        parts = tuple(str(part) for part in command)
        self.exec_commands.append(parts)
        # The collision preflight is the invocation that receives only the target.
        is_preflight = not any("ln " in part for part in parts)
        code = self._preflight_exit_code if is_preflight else self._exit_code
        return ExecResult(stdout=b"", stderr=b"", exit_code=code)

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (data, user)
        self.writes.append(path)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: object = None,
    ) -> None:
        _ = (recursive, user)
        self.removed.append(Path(path))

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: object = None,
    ) -> None:
        _ = (parents, user)
        self.made_dirs.append(Path(path))


@pytest.mark.asyncio
async def test_write_new_file_with_a_bound_user_reports_an_existing_name(tmp_path: Path) -> None:
    """A target that is already visible is rejected before any payload is staged."""
    session = _ExitCodeUnixLocalSession(tmp_path, exit_code=0, preflight_exit_code=13)

    with pytest.raises(FileExistsError):
        await session.write_new_file(
            Path("notes.txt"), io.BytesIO(b"payload"), user=User(name="sandbox-user")
        )

    # No payload bytes were uploaded, so a create onto an occupied name costs one probe
    # rather than a full staged write that is then discarded.
    assert session.writes == []
    assert session.removed == []


@pytest.mark.asyncio
async def test_write_new_file_with_a_bound_user_reports_a_racing_creator(tmp_path: Path) -> None:
    """A creator that wins between the preflight and the link still loses the name."""
    session = _ExitCodeUnixLocalSession(tmp_path, exit_code=13, preflight_exit_code=0)

    with pytest.raises(FileExistsError):
        await session.write_new_file(
            Path("notes.txt"), io.BytesIO(b"payload"), user=User(name="sandbox-user")
        )

    # Here the payload was staged before the race was detected, and the staging entry is
    # still cleaned up rather than left in the workspace.
    assert all(path.name.startswith(".apply-patch-create-") for path in session.writes)
    assert session.removed == session.writes
    assert session.made_dirs != []


@pytest.mark.asyncio
async def test_write_new_file_with_a_bound_user_links_the_completed_payload(
    tmp_path: Path,
) -> None:
    """The payload is written first, then the target name is claimed by linking it."""
    session = _ExitCodeUnixLocalSession(tmp_path, exit_code=0)

    await session.write_new_file(
        Path("notes.txt"), io.BytesIO(b"payload"), user=User(name="sandbox-user")
    )

    staged = session.writes[0]
    assert staged.name.startswith(".apply-patch-create-")
    dispatched = [part for cmd in session.exec_commands for part in cmd]
    assert any("ln " in part for part in dispatched)
    assert any(part.endswith("notes.txt") for part in dispatched)
    assert str(staged) in dispatched
    assert session.removed == [staged]


@pytest.mark.asyncio
async def test_write_new_file_with_a_bound_user_keeps_a_symlink_name_unresolved(
    tmp_path: Path,
) -> None:
    """The exclusive create must act on the link name, not on the target it points at."""
    session = _ExitCodeUnixLocalSession(tmp_path, exit_code=13)
    (tmp_path / "link.txt").symlink_to(tmp_path / "missing.txt")

    with pytest.raises(FileExistsError):
        await session.write_new_file(
            Path("link.txt"), io.BytesIO(b"payload"), user=User(name="sandbox-user")
        )

    dispatched = [part for cmd in session.exec_commands for part in cmd]
    assert any(part.endswith("link.txt") for part in dispatched)
    assert not any(part.endswith("missing.txt") for part in dispatched)


@pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
def test_exclusive_create_script_reports_a_taken_name_on_each_shell(
    shell: str, tmp_path: Path
) -> None:
    """Run the shipped script through real shells.

    The script is dispatched as ``sh -lc``, so whichever shell provides ``/bin/sh``
    decides how a failing command is handled. An earlier version used ``:``, which is a
    POSIX special builtin, so a redirection failure terminated dash before the explicit
    exit mapping ran and the collision surfaced as a generic write error. This lives with
    the Unix-local tests because tests/conftest.py already skips them on Windows.
    """
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not available")

    staging = tmp_path / "staging"
    staging.write_bytes(b"payload")
    taken = tmp_path / "taken.txt"
    taken.write_bytes(b"existing\n")
    dangling = tmp_path / "dangling.txt"
    dangling.symlink_to(tmp_path / "missing.txt")

    def run(target: Path) -> int:
        return subprocess.run(
            [executable, "-c", _EXCLUSIVE_CREATE_SCRIPT, shell, str(target), str(staging)],
            capture_output=True,
        ).returncode

    assert run(taken) == _EXCLUSIVE_CREATE_EXISTS_CODE
    assert taken.read_bytes() == b"existing\n"

    assert run(dangling) == _EXCLUSIVE_CREATE_EXISTS_CODE
    assert not (tmp_path / "missing.txt").exists()

    # The caller creates the parent, so the script only has to claim the name.
    fresh = tmp_path / "nested" / "fresh.txt"
    fresh.parent.mkdir()
    assert run(fresh) == 0
    assert fresh.read_bytes() == b"payload"

    existing_directory = tmp_path / "adir"
    existing_directory.mkdir()
    assert run(existing_directory) == _EXCLUSIVE_CREATE_EXISTS_CODE
    assert list(existing_directory.iterdir()) == []


@pytest.mark.asyncio
async def test_apply_patch_create_through_the_session_rejects_a_dangling_symlink(
    tmp_path: Path,
) -> None:
    """Drive the real caller path.

    WorkspaceEditor normalizes the destination before dispatching, and this backend
    resolves leaf symlinks, so a create aimed at a dangling link used to land on the
    link's absent target and report success.
    """
    session = _exclusive_write_session(tmp_path)
    (tmp_path / "link.txt").symlink_to(tmp_path / "missing.txt")

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            ApplyPatchOperation(type="create_file", path="link.txt", diff="+clobbered\n")
        )

    assert not (tmp_path / "missing.txt").exists()
    assert (tmp_path / "link.txt").is_symlink()


@pytest.mark.asyncio
async def test_apply_patch_create_through_the_session_rejects_a_directory(
    tmp_path: Path,
) -> None:
    session = _exclusive_write_session(tmp_path)
    (tmp_path / "adir").mkdir()

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            ApplyPatchOperation(type="create_file", path="adir", diff="+clobbered\n")
        )

    assert list((tmp_path / "adir").iterdir()) == []


@pytest.mark.asyncio
async def test_apply_patch_create_through_the_session_keeps_existing_content(
    tmp_path: Path,
) -> None:
    session = _exclusive_write_session(tmp_path)
    (tmp_path / "notes.txt").write_bytes(b"important\n")

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            ApplyPatchOperation(type="create_file", path="notes.txt", diff="+clobbered\n")
        )

    assert (tmp_path / "notes.txt").read_bytes() == b"important\n"


@pytest.mark.asyncio
async def test_apply_patch_create_through_the_session_writes_a_new_nested_file(
    tmp_path: Path,
) -> None:
    session = _exclusive_write_session(tmp_path)

    await session.apply_patch(
        ApplyPatchOperation(type="create_file", path="nested/dir/new.txt", diff="+hello\n")
    )

    assert (tmp_path / "nested" / "dir" / "new.txt").read_text() == "hello"
    assert not any(p.name.startswith(".") for p in (tmp_path / "nested" / "dir").iterdir())


@pytest.mark.asyncio
async def test_apply_patch_create_through_the_session_reports_a_file_parent_as_a_write_error(
    tmp_path: Path,
) -> None:
    """A parent that is a regular file is not a collision on the requested name.

    Reporting it as one would tell the model to use update_file for a target that does
    not exist and cannot be updated.
    """
    session = _exclusive_write_session(tmp_path)
    (tmp_path / "parent").write_bytes(b"i am a file\n")

    with pytest.raises(WorkspaceArchiveWriteError):
        await session.apply_patch(
            ApplyPatchOperation(type="create_file", path="parent/child.txt", diff="+hi\n")
        )


@pytest.mark.asyncio
async def test_apply_patch_create_accepts_a_destination_at_the_component_limit(
    tmp_path: Path,
) -> None:
    """Staging must not push a valid destination name past the filesystem's limit.

    Deriving the staging basename from the destination made it longer than the
    destination itself, so a name the ordinary write path accepts failed to create.
    """
    session = _exclusive_write_session(tmp_path)
    long_name = "a" * 250 + ".txt"
    # Confirm the platform really does accept this name, so the test fails for the
    # right reason rather than because the limit is lower here.
    probe = tmp_path / long_name
    probe.write_text("probe")
    probe.unlink()

    await session.apply_patch(
        ApplyPatchOperation(type="create_file", path=long_name, diff="+hello\n")
    )

    assert (tmp_path / long_name).read_text() == "hello"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory write permissions")
@pytest.mark.asyncio
async def test_apply_patch_create_reports_collision_inside_a_read_only_parent(
    tmp_path: Path,
) -> None:
    """A visible collision must classify as a collision, not as a permission failure.

    Staging before classifying meant a target inside an executable but non-writable
    parent failed on the staging write, so the caller was told the write failed instead
    of being told to use update_file.
    """
    session = _exclusive_write_session(tmp_path)
    parent = tmp_path / "locked"
    parent.mkdir()
    target = parent / "notes.txt"
    target.write_bytes(b"important\n")
    parent.chmod(0o555)
    try:
        with pytest.raises(ApplyPatchDiffError):
            await session.apply_patch(
                ApplyPatchOperation(
                    type="create_file", path="locked/notes.txt", diff="+clobbered\n"
                )
            )
        assert target.read_bytes() == b"important\n"
    finally:
        parent.chmod(0o755)
