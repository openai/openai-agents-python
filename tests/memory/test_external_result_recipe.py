"""Exercise the recipes through Runner and independently owned SQLite handles."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agents import Agent, ResumableSession, RunConfig, Runner
from agents.exceptions import UserError
from agents.memory.resumable_session import ResumableSessionError
from agents.testing import ScriptedModel, assistant_message
from examples.memory.resumable_sqlite_session import SQLiteStore
from examples.memory.reviewed_external_result import (
    NoteService,
    Review,
    request_review,
    resume_review,
)

CONFIG = RunConfig(tracing_disabled=True)


def resume_worker(folder: Path, review: Review, barrier) -> str:
    try:
        return asyncio.run(resume_review(folder, review, barrier=barrier))
    except ResumableSessionError as exc:
        return f"not-claimed:{exc.reason}"


@pytest.mark.asyncio
async def test_two_processes_only_one_claims_the_review(tmp_path: Path) -> None:
    review = await request_review(tmp_path)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager, ProcessPoolExecutor(2, mp_context=context) as pool:
        barrier = manager.Barrier(2)
        futures = [pool.submit(resume_worker, tmp_path, review, barrier) for _ in range(2)]
        results = await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
    assert sorted(result.startswith("Published: Hello") for result in results) == [False, True]
    assert sum(result.startswith("not-claimed:") for result in results) == 1
    with closing(sqlite3.connect(tmp_path / "service.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone() == (1,)
    assert NoteService(tmp_path / "service.db").reconcile(review) == "Published: Hello"


@pytest.mark.asyncio
async def test_changed_arguments_require_fresh_review_before_claim(tmp_path: Path) -> None:
    review = await request_review(tmp_path)
    with pytest.raises(PermissionError, match="fresh review"):
        await resume_review(tmp_path, replace(review, arguments='{"text":"changed","expected":""}'))
    session = await ResumableSession.resume(
        SQLiteStore(tmp_path / "sessions.db"), review.session_id
    )
    assert session.is_paused
    assert NoteService(tmp_path / "service.db").reconcile(review) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revoked", "stale-note"])
async def test_current_authority_and_state_are_checked_at_effect(
    tmp_path: Path, reason: str
) -> None:
    review = await request_review(tmp_path)
    service = NoteService(tmp_path / "service.db")
    if reason == "revoked":
        service.allow(review.session_id, False)
    else:
        with closing(sqlite3.connect(service.path)) as db, db:
            db.execute("INSERT INTO notes VALUES (?, ?)", (review.session_id, "Other writer"))
    with pytest.raises(UserError, match="revoked|changed"):
        await resume_review(tmp_path, review)
    if reason == "revoked":
        with pytest.raises(PermissionError, match="revoked"):
            service.reconcile(review)
    else:
        assert service.reconcile(review) is None


@pytest.mark.asyncio
async def test_lost_ack_requires_read_only_reconciliation_not_replay(tmp_path: Path) -> None:
    review = await request_review(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await resume_review(tmp_path, review, lose_ack=True)
    with pytest.raises(ResumableSessionError) as failure:
        await ResumableSession.resume(SQLiteStore(tmp_path / "sessions.db"), review.session_id)
    assert failure.value.reason == "active"
    service = NoteService(tmp_path / "service.db")
    confirmed = service.reconcile(review)
    assert confirmed == "Published: Hello"
    with closing(sqlite3.connect(service.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone() == (1,)
    # A fresh conversation receives the confirmed result without replaying the tool.
    fresh = await ResumableSession.create(SQLiteStore(tmp_path / "sessions.db"))
    agent = Agent(name="Reader", model=ScriptedModel([[assistant_message("Confirmed")]]))
    result = await Runner.run(agent, confirmed, session=fresh, run_config=CONFIG)
    assert result.final_output == "Confirmed"


@pytest.mark.asyncio
async def test_external_operation_key_rejects_changed_args_and_reuses_confirmed_result(
    tmp_path: Path,
) -> None:
    review = await request_review(tmp_path)
    await resume_review(tmp_path, review)
    service = NoteService(tmp_path / "service.db")
    assert service.publish(review, review.arguments) == "Published: Hello"
    changed = replace(review, arguments='{"expected":"","text":"Other"}')
    with pytest.raises(PermissionError, match="different arguments"):
        service.publish(changed, changed.arguments)


@pytest.mark.asyncio
async def test_precompleted_external_result_resumes_without_repeating_write(tmp_path: Path) -> None:
    review = await request_review(tmp_path)
    service = NoteService(tmp_path / "service.db")
    # A trusted reviewer accepts these exact arguments outside the Runner.
    confirmed = service.publish(review, review.arguments)
    with closing(sqlite3.connect(service.path)) as db, db:
        db.execute("UPDATE notes SET text = ?", ("A later authorized edit",))

    assert await resume_review(tmp_path, review) == confirmed
    with closing(sqlite3.connect(service.path)) as db:
        assert db.execute("SELECT text FROM notes").fetchone() == ("A later authorized edit",)
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone() == (1,)


@pytest.mark.asyncio
async def test_reconciliation_rechecks_permission_after_completion(tmp_path: Path) -> None:
    review = await request_review(tmp_path)
    await resume_review(tmp_path, review)
    service = NoteService(tmp_path / "service.db")
    service.allow(review.session_id, False)
    with pytest.raises(PermissionError, match="revoked"):
        service.reconcile(review)
