"""Local goal ownership and persistence through the shipped Python API."""

import asyncio
import gc
import json
import subprocess
import sys
import weakref
from importlib.util import find_spec
from pathlib import Path

import pytest

from agents.models.codex_goals import CodexGoal, open_goal

native = pytest.mark.skipif(
    find_spec("agents.models._codex_native") is None, reason="Requires the native SDK build"
)


def test_goal_accepts_native_fields_and_round_trips_python_names() -> None:
    native_goal = {
        "threadId": "synthetic-root",
        "objective": "Review",
        "status": "budgetLimited",
        "tokenBudget": 64,
        "tokensUsed": 64,
        "timeUsedSeconds": 5,
        "createdAt": 100,
        "updatedAt": 105,
    }
    goal = CodexGoal.model_validate_json(json.dumps(native_goal))
    assert goal.model_dump(by_alias=True) == native_goal
    assert CodexGoal(**goal.model_dump()) == goal
    assert CodexGoal.model_validate_json(goal.model_dump_json()) == goal


@pytest.mark.asyncio
async def test_creation_requires_authority_and_a_profile(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        async with open_goal(tmp_path, thread_id=None):
            pytest.fail()
    with pytest.raises(ValueError):
        async with open_goal(Path("."), thread_id=None, can_write=lambda: True):
            pytest.fail()
    assert not list(tmp_path.iterdir())


@native
@pytest.mark.asyncio
async def test_reopen_in_a_fresh_process(tmp_path: Path) -> None:
    requests = []

    def reject(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(True)
        writer.close()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        (tmp_path / "config.toml").write_text(
            'model_provider="peer"\n[model_providers.peer]\nname="Local"\n'
            f'base_url="http://127.0.0.1:{port}/v1"\nwire_api="responses"\n'
        )
        async with open_goal(tmp_path, thread_id=None, can_write=lambda: True) as root:
            assert await root.get() is None
            first = await root.set_paused("  Review  ")
            assert first.objective == "Review" and first.token_budget is None
            assert first.status == "paused" and first.thread_id == root.thread_id
            assert first.tokens_used == first.time_used_seconds == 0
            assert 0 < first.created_at <= first.updated_at
            await root.set_paused("Revise", token_budget=2**53 + 1)
            last = await root.set_paused("Verify")
            assert last.token_budget == 2**53 + 1
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            """
import asyncio, sys
from pathlib import Path
from agents.models.codex_goals import open_goal
async def read():
    async with open_goal(Path(sys.argv[1]), thread_id=sys.argv[2]) as root:
        print((await root.get()).model_dump_json(by_alias=True))
asyncio.run(read())
""",
            str(tmp_path),
            root.thread_id,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await asyncio.wait_for(child.communicate(), 30)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert child.returncode == 0
        assert CodexGoal.model_validate_json(output) == last
        assert requests == []


@native
@pytest.mark.asyncio
async def test_authority_and_exclusive_owner(tmp_path: Path) -> None:
    allowed = True

    def can_write() -> bool:
        return allowed

    ref = weakref.ref(can_write)
    async with open_goal(tmp_path, thread_id=None, can_write=can_write) as root:
        with pytest.raises(RuntimeError):
            await asyncio.create_task(root.get())
        with pytest.raises(RuntimeError):
            async with open_goal(tmp_path, thread_id=root.thread_id):
                pytest.fail()
        for bad in (0, -1, True, 2**63):
            with pytest.raises(ValueError):
                await root.set_paused("Check", token_budget=bad)
        for objective in ("  ", "x" * 4001):
            with pytest.raises(ValueError):
                await root.set_paused(objective)
        allowed = False
        with pytest.raises(PermissionError):
            await root.set_paused("Denied")
        assert await root.get() is None
    del can_write
    gc.collect()
    assert ref() is None
    with pytest.raises(RuntimeError):
        await root.get()
    async with open_goal(tmp_path, thread_id=root.thread_id) as reopened:
        assert await reopened.get() is None
        with pytest.raises(PermissionError):
            await reopened.set_paused("Denied")
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(RuntimeError):
        async with open_goal(other, thread_id=root.thread_id):
            pytest.fail()
    assert not list(other.glob("*.sqlite"))


@native
@pytest.mark.asyncio
@pytest.mark.parametrize("original", [None, 64])
async def test_retained_budget_must_fit_current_profile(
    tmp_path: Path, original: int | None
) -> None:
    config = tmp_path / "config.toml"
    if original:
        config.write_text(f"[goals]\nmax_goal_token_budget={original}\n")
    async with open_goal(tmp_path, thread_id=None, can_write=lambda: True) as root:
        goal = await root.set_paused("Original")
        assert goal.token_budget == original
    config.write_text("[goals]\nmax_goal_token_budget=8\n")
    async with open_goal(tmp_path, thread_id=root.thread_id, can_write=lambda: True) as reopened:
        for budget in (None, 10):
            with pytest.raises(ValueError):
                await reopened.set_paused("Over limit", token_budget=budget)
        assert await reopened.get() == goal
        assert (await reopened.set_paused("Within limit", token_budget=8)).token_budget == 8
    assert "Over limit" not in next(tmp_path.rglob("*.jsonl")).read_text()


# Synthetic states that an idle editor cannot create.
def write_fixture(db: Path, sql: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys; db=sqlite3.connect(sys.argv[1]); "
            "db.executescript(sys.argv[2]); db.close()",
            str(db),
            sql,
        ],
        check=True,
        timeout=10,
    )


@native
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [0, -1])
async def test_invalid_retained_budget_requires_correction(tmp_path: Path, invalid: int) -> None:
    async with open_goal(tmp_path, thread_id=None, can_write=lambda: True) as root:
        await root.set_paused("Original", token_budget=8)
    write_fixture(
        next(tmp_path.glob("goals_*.sqlite")), f"UPDATE thread_goals SET token_budget={invalid}"
    )
    async with open_goal(tmp_path, thread_id=root.thread_id, can_write=lambda: True) as live:
        with pytest.raises(ValueError):
            await live.set_paused("Unsafe")
        assert (await live.set_paused("Corrected", token_budget=4)).token_budget == 4
    assert "Unsafe" not in next(tmp_path.rglob("*.jsonl")).read_text()


@native
@pytest.mark.asyncio
async def test_native_status_and_rejected_storage_write(tmp_path: Path) -> None:
    async with open_goal(tmp_path, thread_id=None, can_write=lambda: True) as root:
        await root.set_paused("Original", token_budget=8)
    db = next(tmp_path.glob("goals_*.sqlite"))

    write_fixture(db, "UPDATE thread_goals SET status='active'")
    async with open_goal(tmp_path, thread_id=root.thread_id, can_write=lambda: True) as live:
        with pytest.raises(ValueError):
            await live.set_paused("Unsafe")
    write_fixture(
        db,
        "UPDATE thread_goals SET status='budget_limited'; "
        "CREATE TRIGGER reject_write BEFORE UPDATE ON thread_goals "
        "WHEN NEW.objective='Rejected' BEGIN SELECT RAISE(ABORT, 'fixture'); END",
    )
    async with open_goal(tmp_path, thread_id=root.thread_id, can_write=lambda: True) as live:
        goal = await live.set_paused("Limited")
        assert goal.status == "budgetLimited"
        with pytest.raises(RuntimeError):
            await live.set_paused("Rejected")
        assert await live.get() == goal
    rollout = next(tmp_path.rglob("*.jsonl")).read_text()
    assert "Rejected" in rollout and "Unsafe" not in rollout
    assert rollout.count('"thread_goal_updated"') == 2


@native
@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_repeated_cancellation_joins_one_edit_before_releasing_writer(
    tmp_path: Path, reject: bool
) -> None:
    async with open_goal(tmp_path, thread_id=None, can_write=lambda: True) as root:
        tid = root.thread_id
        await root.set_paused("Original")
    if reject:
        write_fixture(
            next(tmp_path.glob("goals_*.sqlite")),
            "CREATE TRIGGER reject_write BEFORE UPDATE ON thread_goals "
            "BEGIN SELECT RAISE(ABORT, 'fixture'); END",
        )

    async def update() -> None:
        async with open_goal(tmp_path, thread_id=tid, can_write=lambda: True) as thread:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            assert task is not None
            loop.call_soon(task.cancel)
            loop.call_soon(loop.call_soon, task.cancel)
            await thread.set_paused("Once")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(update())
    async with open_goal(tmp_path, thread_id=tid) as reopened:
        goal = await reopened.get()
        assert goal is not None and goal.objective == ("Original" if reject else "Once")
    rows = [
        json.loads(line)["payload"]
        for line in next(tmp_path.rglob("*.jsonl")).read_text().splitlines()
    ]
    assert sum(row.get("type") == "thread_goal_updated" for row in rows) == (1 if reject else 2)
    assert sum(row.get("role") == "user" for row in rows) == 2


@native
@pytest.mark.asyncio
async def test_wrong_origin_and_lost_database_fail_closed(tmp_path: Path) -> None:
    async with open_goal(tmp_path, thread_id=None, can_write=lambda: True) as root:
        goal = await root.set_paused("Original")
    path = next(tmp_path.rglob("*.jsonl"))
    original = path.read_text()
    path.write_text(original.replace("agents-sdk-goal", "another-app"))
    with pytest.raises(ValueError):
        async with open_goal(tmp_path, thread_id=root.thread_id):
            pytest.fail()
    path.write_text(original)
    async with open_goal(tmp_path, thread_id=root.thread_id) as restored:
        assert await restored.get() == goal
    db = next(tmp_path.glob("goals_*.sqlite"))
    db.rename(db.with_suffix(".saved"))
    with pytest.raises(RuntimeError):
        async with open_goal(tmp_path, thread_id=root.thread_id):
            pytest.fail()
    assert not db.exists()


@pytest.mark.asyncio
async def test_cancelled_open_releases_lease_even_with_retained_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = asyncio.Event()

    class Lease:
        async def close(self) -> None:
            await asyncio.sleep(0)
            closed.set()

    refs = []

    class Native:
        @staticmethod
        def open_goal(*args: object) -> asyncio.Future[Lease]:
            lease = Lease()
            refs.append(weakref.ref(lease))
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            task = asyncio.current_task()
            assert task is not None
            loop.call_soon(task.cancel)
            loop.call_soon(future.set_result, lease)
            return future

    monkeypatch.setattr("agents.models.codex_goals.import_module", lambda _: Native)

    async def enter() -> None:
        async with open_goal(tmp_path, thread_id="saved"):
            pytest.fail("Cancelled entry published a lease")

    with pytest.raises(asyncio.CancelledError) as retained:
        await asyncio.create_task(enter())
    assert closed.is_set()
    await asyncio.sleep(0)
    gc.collect()
    assert retained.value.__traceback__ is not None
    assert refs[0]() is None
