"""Exercise an external thumbnail order through the native Session and application store."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agents import NativeError
from agents.experimental.deferred import JobBinding
from agents.state import SessionState
from examples.external_job.__main__ import ThumbnailProvider, run_order
from examples.external_job.journal import Busy, Journal
from examples.external_job.store import ThumbnailStore
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider

pytestmark = pytest.mark.skipif(os.name != "posix", reason="The example uses a POSIX file lock.")


@pytest.mark.parametrize("options", [[], ["--directory", ""], ["--directory", "relative"]])
def test_worker_requires_host_chosen_state_directory(tmp_path: Path, options: list[str]) -> None:
    worker = subprocess.run(
        [sys.executable, "-m", "examples.external_job", "worker", "new-order", *options],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=8,
        check=False,
    )
    assert worker.returncode != 0
    if not options:
        assert b"--directory" in worker.stderr
    assert list(tmp_path.iterdir()) == []


def test_store_requires_absolute_path_and_existing_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError):
        ThumbnailStore(Path("relative"), "studio")
    with pytest.raises(PermissionError):
        ThumbnailStore(tmp_path / "unselected-parent" / "state", "studio")
    assert list(tmp_path.iterdir()) == []


def test_store_rejects_a_directory_owned_by_another_effective_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "synthetic-private-state"
    directory.mkdir(mode=0o700)
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(PermissionError) as failure:
        ThumbnailStore(directory, "synthetic-private-tenant")
    exc = failure.value
    assert "synthetic-private" not in str(exc) + repr(exc) + repr(exc.args)
    assert exc.__cause__ is None and exc.__context__ is None
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "exposed", "file"])
def test_private_directory_refuses_unsafe_existing_leaf(tmp_path: Path, kind: str) -> None:
    leaf = tmp_path / "synthetic-private-state"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if kind == "symlink":
        leaf.symlink_to(outside, target_is_directory=True)
    elif kind == "exposed":
        leaf.mkdir()
        leaf.chmod(0o755)
    else:
        leaf.touch()
    with pytest.raises(PermissionError) as failure:
        ThumbnailStore(leaf, "synthetic-private-tenant")
    exc = failure.value
    assert "synthetic-private" not in str(exc) + repr(exc) + repr(exc.args)
    assert exc.__cause__ is None and exc.__context__ is None
    assert not (outside / "orders.sqlite3").exists()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("orders.sqlite3", "symlink"),
        ("orders.sqlite3", "hardlink"),
        ("orders.sqlite3", "exposed"),
        ("lock", "symlink"),
        ("lock", "hardlink"),
        ("lock", "exposed"),
        ("orders.sqlite3-journal", "hardlink"),
        ("orders.sqlite3-wal", "symlink"),
        ("orders.sqlite3-shm", "exposed"),
    ],
)
def test_private_store_rejects_unsafe_database_sidecar_and_lock(
    tmp_path: Path, name: str, kind: str
) -> None:
    directory = tmp_path / "synthetic-private-state"
    store = ThumbnailStore(directory, "studio")
    journal = Journal(store, "synthetic-private-order")
    name = journal.key + ".lock" if name == "lock" else name
    leaf = directory / name
    if leaf.exists():
        leaf.unlink()
    outside = tmp_path / "outside"
    outside.touch()
    outside.chmod(0o600)
    if kind == "symlink":
        leaf.symlink_to(outside)
    elif kind == "hardlink":
        leaf.hardlink_to(outside)
    else:
        leaf.touch()
        leaf.chmod(0o644)
    with pytest.raises(PermissionError) as failure:
        if name.endswith(".lock"):
            with journal.owner():
                pytest.fail("An unsafe lock allowed ownership.")
        else:
            with store.transaction():
                pytest.fail("An unsafe file allowed a database transaction.")
    exc = failure.value
    assert "synthetic-private" not in str(exc) + repr(exc) + repr(exc.args)
    assert exc.__cause__ is None and exc.__context__ is None
    assert outside.read_bytes() == b""


def test_new_store_files_are_private_and_reusable(tmp_path: Path) -> None:
    directory = tmp_path / "synthetic private?mode=ro & receipts#100%"
    old_umask = os.umask(0)
    try:
        store = ThumbnailStore(directory, "studio")
        journal = Journal(store, "one")
        with journal.owner():
            assert journal.load() == (None, False)
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO thumbnails VALUES (?, ?, ?, ?, ?)",
                    ("studio", "one", "blue", "synthetic-private-receipt", None),
                )
                assert (directory / "orders.sqlite3-journal").stat().st_mode & 0o077 == 0
    finally:
        os.umask(old_umask)
    assert directory.stat().st_mode & 0o077 == 0
    assert (directory / "orders.sqlite3").stat().st_mode & 0o077 == 0
    assert (directory / (journal.key + ".lock")).stat().st_mode & 0o077 == 0
    with Journal(ThumbnailStore(directory, "studio"), "one").owner():
        pass


def test_store_refuses_vfs_that_does_not_apply_private_creation_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "synthetic-private-state"
    connect = sqlite3.connect

    def without_unix_modeof(_database: str, *, timeout: float, uri: bool) -> sqlite3.Connection:
        return connect(directory / "orders.sqlite3", timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", without_unix_modeof)
    old_umask = os.umask(0)
    try:
        with pytest.raises(PermissionError) as failure:
            ThumbnailStore(directory, "synthetic-private-tenant")
    finally:
        os.umask(old_umask)
    exc = failure.value
    assert "synthetic-private" not in str(exc) + repr(exc) + repr(exc.args)
    assert exc.__cause__ is None and exc.__context__ is None
    assert (directory / "orders.sqlite3").read_bytes() == b""
    assert not (directory / "orders.sqlite3-journal").exists()


def database_lock_observation(path: Path) -> bytes:
    """Ask an independent process whether it can acquire the SQLite writer lock."""
    observer = subprocess.run(
        [
            sys.executable,
            "-c",
            """import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0)
try:
    db.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError:
    print("locked")
else:
    print("acquired")
""",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=5,
    )
    return observer.stdout


def test_failed_connection_preserves_another_orders_transaction_lock(tmp_path: Path) -> None:
    store = ThumbnailStore(tmp_path, "studio")
    with store.transaction() as first:
        first.execute(
            "INSERT INTO thumbnails VALUES (?, ?, ?, ?, ?)",
            ("studio", "one", "blue", "synthetic-private-receipt", None),
        )
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            ThumbnailStore(tmp_path, "another-studio")
        assert database_lock_observation(tmp_path / "orders.sqlite3") == b"locked\n"


async def test_order_reports_text_only_native_completion_before_cli_deadline(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    store = ThumbnailStore(tmp_path, "studio")
    journal = Journal(store, "no-thumbnail")
    model = provider(responses)
    responses.respond(assistant_output("A thumbnail cannot be made."))
    async with timeout(3):
        try:
            with pytest.raises(
                RuntimeError, match=r"^The model finished without requesting a thumbnail\.$"
            ):
                await run_order(journal, ThumbnailProvider(journal), model)
            assert len(responses.requests) == 1
            owner = Journal(store, "no-thumbnail")
            with owner.owner():
                assert owner.load() == (None, False)
            with store.transaction() as db:
                assert db.execute("SELECT count(*) FROM thumbnails").fetchone() == (0,)
        finally:
            await model.aclose()


async def test_order_preserves_native_failure_and_releases_owner(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    store = ThumbnailStore(tmp_path, "studio")
    journal = Journal(store, "invalid-thumbnail")
    model = provider(responses)
    responses.respond(function_call("render_thumbnail", {"color": "green"}, "invalid-color"))
    async with timeout(3):
        try:
            with pytest.raises(NativeError, match="Manual tool arguments could not be delivered"):
                await run_order(journal, ThumbnailProvider(journal), model)
            assert len(responses.requests) == 1
            owner = Journal(store, "invalid-thumbnail")
            with owner.owner():
                assert owner.load() == (None, False)
            with store.transaction() as db:
                assert db.execute("SELECT count(*) FROM thumbnails").fetchone() == (0,)
        finally:
            await model.aclose()


async def test_worker_result_resumes_one_original_call(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    store = ThumbnailStore(tmp_path, "local-studio")
    journal = Journal(store, "order-one")
    model = provider(responses)
    responses.respond(function_call("render_thumbnail", {"color": "blue"}, "original-call"))
    responses.respond(assistant_output("The thumbnail contains 3085 bytes."))
    try:
        async with timeout(20):
            assert await run_order(journal, ThumbnailProvider(journal), model) == "pending"
            assert len(responses.requests) == 1
            with journal.owner():
                raw, finished = journal.load()
                assert raw and not finished
                original = SessionState.from_json(raw)
                assert original.native_continuation is not None
                assert original.native_continuation.pending_manual[0].call_id == "original-call"
            worker = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "examples.external_job",
                    "worker",
                    "order-one",
                    "--directory",
                    str(tmp_path),
                ],
                env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=8,
                check=True,
            )
            assert (worker.stdout, worker.stderr) == (b"Rendered.\n", b"")
            assert not store.work("order-one")
            restored = Journal(ThumbnailStore(tmp_path, "local-studio"), "order-one")
            assert await run_order(restored, ThumbnailProvider(restored), model) == "completed"
            assert await run_order(restored, ThumbnailProvider(restored), model) == "completed"
    finally:
        await model.aclose()
    assert len(responses.requests) == 2
    outputs = [i for i in responses.requests[-1]["input"] if i["type"] == "function_call_output"]
    assert len(outputs) == 1 and outputs[0]["call_id"] == "original-call"
    assert json.loads(outputs[0]["output"]) == {"size_bytes": 3085}
    with store.transaction() as db:
        receipt, image = db.execute("SELECT receipt, image FROM thumbnails").fetchone()
        result, finished_state = db.execute("SELECT result, finished FROM orders").fetchone()
    assert image == b"P6\n32 32\n255\n" + bytes((0, 0, 255)) * 1024
    assert json.loads(result) == {"size_bytes": len(image)}
    assert receipt not in json.dumps(responses.requests) + raw + finished_state


async def test_revoked_order_refuses_worker_and_resume(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    store = ThumbnailStore(tmp_path, "studio")
    journal = Journal(store, "order-revoked")
    model = provider(responses)
    responses.respond(function_call("render_thumbnail", {"color": "red"}, "render-call"))
    try:
        async with timeout(20):
            assert await run_order(journal, ThumbnailProvider(journal), model) == "pending"
            with journal.owner(), pytest.raises(Busy):
                with Journal(store, "order-revoked").owner():
                    pytest.fail("A second host acquired the active order.")
            with store.transaction() as db:
                saved_binding = db.execute("SELECT binding FROM orders").fetchone()[0]
            with journal.owner():
                stale = await journal.claim(JobBinding(**json.loads(saved_binding)), "advance")
            with journal.owner(), pytest.raises(PermissionError):
                await ThumbnailProvider(journal).observe(stale)
            with store.transaction() as db:
                db.execute(
                    "UPDATE orders SET allowed=0 WHERE tenant=? AND order_id=?", journal.scope
                )
            assert not store.work("order-revoked")
            with pytest.raises(PermissionError):
                await run_order(journal, ThumbnailProvider(journal), model)
    finally:
        await model.aclose()
    assert len(responses.requests) == 1


async def test_identical_requests_have_independent_scoped_business_orders(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    model = provider(responses)
    try:
        async with timeout(25):
            for tenant, order in [("studio", "one"), ("studio", "two"), ("other-studio", "one")]:
                store = ThumbnailStore(tmp_path, tenant)
                app = Journal(store, order)
                responses.respond(function_call("render_thumbnail", {"color": "blue"}, "same-call"))
                responses.respond(assistant_output("Done."))
                assert await run_order(app, ThumbnailProvider(app), model) == "pending"
                assert store.work(order)
                assert await run_order(app, ThumbnailProvider(app), model) == "completed"
                assert await run_order(app, ThumbnailProvider(app), model) == "completed"
    finally:
        await model.aclose()
    assert len(responses.requests) == 6
    with ThumbnailStore(tmp_path, "studio").transaction() as db:
        assert db.execute("SELECT count(DISTINCT receipt) FROM thumbnails").fetchone() == (3,)
        assert db.execute("SELECT count(*) FROM orders WHERE finished IS NOT NULL").fetchone() == (
            3,
        )


async def test_documented_model_endpoint_runs_three_cli_commands(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root / "packages" / "codex")}
    env.pop("OPENAI_API_KEY", None)
    endpoint = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "examples.external_job.model",
        cwd=root,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert endpoint.stdout is not None
        try:
            ready = await asyncio.wait_for(endpoint.stdout.readline(), 30)
        except asyncio.TimeoutError:
            if endpoint.returncode is None:
                endpoint.kill()
            stdout, stderr = await asyncio.wait_for(endpoint.communicate(), 5)
            pytest.fail(
                f"Model endpoint startup timed out (exit={endpoint.returncode}, "
                f"stdout={stdout[-4096:]!r}, stderr={stderr[-4096:]!r})"
            )
        assert ready.startswith(b"MODEL_PORT="), ready
        port = int(ready.removeprefix(b"MODEL_PORT="))
        directory = tmp_path / "private-state"
        for command, expected in (
            ("run", b"pending\n"),
            ("worker", b"Rendered.\n"),
            ("run", b"completed\n"),
        ):
            options = ["--model-port", str(port)] if command == "run" else []
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "examples.external_job",
                command,
                "sample-01",
                *options,
                "--directory",
                str(directory),
                cwd=root,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 40)
                assert child.returncode == 0, (stdout, stderr)
                assert stdout == expected
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
        stdout, stderr = await asyncio.wait_for(endpoint.communicate(), 15)
        assert endpoint.returncode == 0, (stdout, stderr)
        assert stdout == b"Verified thumbnail result; model endpoint closed.\n"
        with sqlite3.connect(directory / "orders.sqlite3") as db:
            assert db.execute("SELECT order_id, length(image) FROM thumbnails").fetchall() == [
                ("sample-01", 3085)
            ]
            (result,) = db.execute("SELECT result FROM orders").fetchone()
            assert json.loads(result) == {"size_bytes": 3085}
    finally:
        if endpoint.returncode is None:
            endpoint.kill()
            await endpoint.wait()
