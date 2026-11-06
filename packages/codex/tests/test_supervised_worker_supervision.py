"""Check artifact verification and turn cleanup without an executor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents import Turn
from examples.supervised_worker import __main__ as worker
from tests._asyncio import timeout


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Uses Unix artifact flags")
@pytest.mark.parametrize(
    ("name_length", "trailing", "accepted"),
    [(117, b"extra bytes\n", False), (118, b"", True)],
    ids=["reject-trailing-bytes", "accept-complete-long-artifact"],
)
def test_worker_verifies_complete_artifact(
    tmp_path: Path, name_length: int, trailing: bytes, accepted: bool
) -> None:
    active_worker = worker.Worker(tmp_path / ("a" * name_length), False)
    active_worker.workspace.mkdir(parents=True)
    expected = active_worker.expected_text
    (active_worker.workspace / worker.ARTIFACT).write_bytes(expected.encode() + trailing)

    assert active_worker.read_artifact() == (expected if accepted else None)


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["caller-cancel", "both-fail"])
async def test_worker_preserves_primary_error_when_joining_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, cleanup_fails: bool
) -> None:
    observer_started = asyncio.Event()
    observer_closed = asyncio.Event()
    cancel_joined = asyncio.Event()
    primary = ValueError("could not read the turn result")
    cause = LookupError("result source failed")
    cleanup = RuntimeError("could not finish cancellation")

    async def events() -> AsyncIterator[object]:
        observer_started.set()
        try:
            yield await asyncio.Future()
        finally:
            observer_closed.set()

    async def wait_for_attention() -> object:
        await observer_started.wait()
        if cleanup_fails:
            raise primary from cause
        return await asyncio.Future()

    async def cancel() -> None:
        await asyncio.sleep(0)
        cancel_joined.set()
        if cleanup_fails:
            raise cleanup

    turn = cast(
        Turn[str],
        SimpleNamespace(events=events, wait_for_attention=wait_for_attention, cancel=cancel),
    )
    trace = worker.TurnTrace(worker.monotonic())
    async with timeout(5):
        task = asyncio.create_task(
            worker.supervise(worker.Worker(tmp_path, False), turn, trace, timeout=4)
        )
        await observer_started.wait()
        if cleanup_fails:
            with pytest.raises(ValueError) as caught:
                await task
            assert caught.value is primary
            assert caught.value.__cause__ is cause
            assert any(
                record.exc_info and record.exc_info[1] is cleanup for record in caplog.records
            )
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not caplog.records

    assert cancel_joined.is_set()
    assert observer_closed.is_set()


@pytest.mark.parametrize(
    ("failure", "output_suffix"),
    [(None, ""), ("session", ""), ("provider", ""), ("cancel", ""), (None, "\n"), ("retarget", "")],
    ids=[
        "success",
        "session-close",
        "provider-close",
        "cancel-close",
        "stale-check",
        "retarget-directory",
    ],
)
@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Uses Unix artifact flags")
async def test_worker_accepts_only_after_owned_resources_close(
    tmp_path, monkeypatch, failure, output_suffix
):
    from argparse import Namespace

    closing, release = asyncio.Event(), asyncio.Event()
    order = []
    cleanup = RuntimeError("Could not close the owned resource")
    directory = tmp_path / "alpha"

    class Session:
        id = "session"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            order.append("session")
            closing.set()
            await release.wait()
            if failure == "session":
                raise cleanup

        async def submit(self, *args, **kwargs):
            (directory / "workspace" / worker.ARTIFACT).write_text("alpha: verified\n")
            return SimpleNamespace(turn_id="turn")

        async def items(self):
            yield {
                "type": "command_execution",
                "turn_id": "turn",
                "status": "completed",
                "exit_code": 0,
                "cwd": (directory / "workspace").as_uri(),
                "argv": [worker.VERIFY_COMMAND],
                "output": "alpha: verified\n" + output_suffix,
                "id": "check",
            }

        async def turns(self):
            yield {"id": "turn", "receipt": {"verified": True}}

        def rate_limits(self):
            return SimpleNamespace(requests=None)

    class Provider:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            order.append("provider")
            if failure == "provider":
                raise cleanup

    class Agent:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, **kwargs):
            pass

        def session(self, **kwargs):
            return Session()

    async def supervise(active_worker, turn, trace, **kwargs):
        trace.steering.extend(["correction"] * len(worker.CORRECTIONS))
        return SimpleNamespace(
            final_output=worker.ArtifactResult(artifact="summary.txt", check="artifact checked"),
            usage=None,
        )

    monkeypatch.setattr(worker, "NativeProvider", Provider)
    monkeypatch.setattr(worker, "Agent", Agent)
    monkeypatch.setattr(worker, "supervise", supervise)
    args = Namespace(
        command="work",
        directory=directory,
        pause_after_effect=False,
        mcp_url=None,
        startup_timeout=5,
        turn_timeout=5,
        instruction_file=[],
    )
    other = tmp_path / "bravo"
    if failure == "retarget":
        other.mkdir()
        args.directory = tmp_path / "worker-link"
        args.directory.symlink_to(directory, target_is_directory=True)
    task = asyncio.create_task(worker.run_worker(args))
    try:
        await asyncio.wait_for(closing.wait(), 5)
        assert worker.read_record(directory, "accepted") is None
        monkeypatch.setattr(sys, "argv", ["worker", "publish", str(directory)])
        with pytest.raises(SystemExit) as publication:
            worker.main()
        assert publication.value.code == 2
        assert worker.read_record(directory, "published") is None
        if failure == "retarget":
            args.directory.unlink()
            args.directory.symlink_to(other, target_is_directory=True)
        if failure == "cancel":
            task.cancel()
        else:
            release.set()
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure in {"session", "provider"}:
            with pytest.raises(RuntimeError) as caught:
                await task
            assert caught.value is cleanup
        else:
            result = await task
            assert result["status"] == ("rejected" if output_suffix else "accepted")
            if not output_suffix:
                assert worker.read_record(directory, "accepted") == {
                    key: value for key, value in result.items() if key != "status"
                }
        assert order == ["session", "provider"]
        if failure in {"session", "provider", "cancel"} or output_suffix:
            assert worker.read_record(directory, "accepted") is None
        if failure == "retarget":
            assert worker.read_record(other, "accepted") is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("read_fails", [False, True])
@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Uses Unix artifact flags")
def test_worker_artifact_close_keeps_primary_read_error(tmp_path, monkeypatch, caplog, read_fails):
    active_worker = worker.Worker(tmp_path, False)
    active_worker.workspace.mkdir()
    (active_worker.workspace / worker.ARTIFACT).write_text(active_worker.expected_text)
    primary = ValueError("Could not read artifact")
    cleanup = OSError("Could not close artifact")
    fdopen = os.fdopen
    closes = []

    class Artifact:
        def __init__(self, fd, mode):
            self.file = fdopen(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size):
            if read_fails:
                raise primary
            return self.file.read(size)

        def close(self):
            closes.append(True)
            self.file.close()
            raise cleanup

    monkeypatch.setattr(os, "fdopen", Artifact)
    with pytest.raises(ValueError if read_fails else OSError) as caught:
        active_worker.read_artifact()
    assert caught.value is (primary if read_fails else cleanup)
    assert closes == [True]
    if read_fails:
        assert any(record.exc_info and record.exc_info[1] is cleanup for record in caplog.records)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Uses Unix directory fsync")
async def test_prior_accepted_snapshot_remains_publishable(tmp_path, monkeypatch):
    from argparse import Namespace

    directory = tmp_path / "alpha"
    directory.mkdir()
    text = "alpha: verified\n"
    accepted = {
        "session_id": "previous",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    worker.record_once(directory, "identity", {"session_id": "previous"})
    worker.record_once(directory, "accepted", accepted)
    with pytest.raises(ValueError, match="fresh worker directory"):
        await worker.run_worker(
            Namespace(command="work", directory=directory, pause_after_effect=False)
        )
    assert worker.read_record(directory, "accepted") == accepted
    monkeypatch.setattr(sys, "argv", ["worker", "publish", str(directory)])
    worker.main()
    assert worker.read_record(directory, "published") == accepted


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Uses Unix directory fsync")
def test_publish_retry_waits_for_directory_sync(tmp_path, monkeypatch, capsys):
    accepted = {"session_id": "previous", "text": "alpha: verified\n"}
    worker.record_once(tmp_path, "accepted", accepted)
    monkeypatch.setattr(sys, "argv", ["worker", "publish", str(tmp_path)])
    fsync = os.fsync
    directory_syncs = 0

    def fail_directory_sync_twice(fd):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs += 1
            if directory_syncs <= 2:
                raise OSError("Could not sync the publication directory")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync_twice)
    for _ in range(2):
        with pytest.raises(OSError, match="Could not sync the publication directory"):
            worker.main()
        assert capsys.readouterr().out == ""
        assert worker.read_record(tmp_path, "published") == accepted

    worker.main()
    assert json.loads(capsys.readouterr().out) == {"status": "already_published"}
    assert directory_syncs == 3
    assert worker.read_record(tmp_path, "published") == accepted


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Retargets a directory symlink")
@pytest.mark.parametrize("command", ["status", "publish"])
def test_worker_cli_keeps_original_journal_on_retarget(tmp_path, monkeypatch, capsys, command):
    directory, other = tmp_path / "alpha", tmp_path / "bravo"
    for path in (directory, other):
        path.mkdir()
        text = f"{path.name}: verified\n"
        worker.record_once(path, "identity", {"session_id": path.name})
        worker.record_once(
            path,
            "accepted",
            {
                "session_id": path.name,
                "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
        )
    original_read = worker.read_record
    accepted = original_read(directory, "accepted")
    alias = tmp_path / "worker-link"
    alias.symlink_to(directory, target_is_directory=True)
    reads = []

    def read_record(path, name):
        value = original_read(path, name)
        if not reads:
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
        reads.append(name)
        return value

    monkeypatch.setattr(worker, "read_record", read_record)
    monkeypatch.setattr(sys, "argv", ["worker", command, str(alias)])
    worker.main()
    result = json.loads(capsys.readouterr().out)
    if command == "status":
        assert result["identity"] == {"session_id": "alpha"}
        assert result["accepted"] == accepted
    else:
        assert result["status"] == "published"
        assert original_read(directory, "published") == accepted
        assert original_read(other, "published") is None
