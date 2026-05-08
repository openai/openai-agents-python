from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agents.sandbox.errors import ExecTimeoutError, PtySessionNotFoundError
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
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


class TestUnixLocalPty:
    @pytest.mark.asyncio
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


class TestUnixLocalExecTimeoutCleanup:
    @pytest.mark.asyncio
    async def test_timeout_reaps_subprocess_so_returncode_is_set(self, tmp_path: Path) -> None:
        """After timeout, the killed subprocess must be awaited so its
        returncode is populated and the asyncio transport is released."""
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))

        # Capture the asyncio.subprocess.Process used by _exec_internal so we can
        # assert it was reaped (returncode populated) after the timeout fires.
        captured: list[asyncio.subprocess.Process] = []
        original_create = asyncio.create_subprocess_exec

        async def _capture_create(
            *args: Any, **kwargs: Any
        ) -> asyncio.subprocess.Process:
            proc = await original_create(*args, **kwargs)
            captured.append(proc)
            return proc

        async with await client.create(manifest=manifest, snapshot=None, options=None) as session:
            with patch.object(asyncio, "create_subprocess_exec", _capture_create):
                with pytest.raises(ExecTimeoutError):
                    await session.exec("sh", "-c", "sleep 30", timeout=0.1)

        assert len(captured) == 1
        proc = captured[0]
        # Without the explicit reap-after-kill the asyncio.Process can outlive
        # _exec_internal with returncode still None, leaking the transport.
        assert proc.returncode is not None
        assert proc.returncode != 0  # killed by SIGKILL

    @pytest.mark.asyncio
    async def test_timeout_skips_killpg_for_already_exited_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the subprocess exited before the timeout handler runs, we must not
        signal its (potentially reused) pid."""
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(tmp_path / "workspace"))

        killpg_calls: list[int] = []

        def _record_killpg(pid: int, sig: int) -> None:
            killpg_calls.append(pid)

        monkeypatch.setattr(os, "killpg", _record_killpg)

        original_wait_for = asyncio.wait_for

        async def _wait_for_with_simulated_exit(
            awaitable: Any, timeout: float | None
        ) -> Any:
            # First call (proc.communicate) — race the timeout: let the
            # subprocess exit naturally and *then* surface a TimeoutError.
            # Use asyncio.TimeoutError explicitly: on Python 3.10 it is a
            # distinct class from the builtin TimeoutError, and the source's
            # `except asyncio.TimeoutError` clause must catch what we raise.
            if not killpg_calls and timeout is not None and timeout < 1.0:
                # Drive the subprocess to completion (sh -c "true" returns fast),
                # then raise to enter the timeout handler.
                try:
                    await original_wait_for(awaitable, timeout=5.0)
                except Exception:
                    pass
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout=timeout)

        async with await client.create(manifest=manifest, snapshot=None, options=None) as session:
            with patch.object(asyncio, "wait_for", _wait_for_with_simulated_exit):
                with pytest.raises(ExecTimeoutError):
                    await session.exec("sh", "-c", "true", timeout=0.05)

        assert killpg_calls == []
