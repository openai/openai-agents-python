from __future__ import annotations

import asyncio
import json

import pytest

from agents import ResumableSession, ResumableSessionError
from agents.memory import ResumableSession as MemoryResumableSession


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        self.fail_load = False
        self.fail_save: str | None = None

    async def load(self, session_id: str) -> str | None:
        if self.fail_load:
            raise RuntimeError("synthetic-private-store-detail")
        return self.records.get(session_id)

    async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
        if self.fail_save == "before":
            raise RuntimeError("synthetic-private-store-detail")
        if self.records.get(session_id) != expected:
            return False
        self.records[session_id] = value
        if self.fail_save == "after":
            raise RuntimeError("synthetic-private-store-detail")
        return True


@pytest.mark.asyncio
async def test_create_allocates_and_confirms_an_independent_id() -> None:
    store = MemoryStore()
    first = await ResumableSession.create(store)
    second = await ResumableSession.create(store)
    assert first.session_id != second.session_id
    assert (await ResumableSession.resume(store, first.session_id)).session_id == first.session_id
    assert MemoryResumableSession is ResumableSession
    record = json.loads(store.records[first.session_id])
    assert record.pop("revision")
    assert record == {"version": 1, "status": "ready", "history": [], "run_state": None}


@pytest.mark.asyncio
async def test_resume_does_not_create_a_missing_id() -> None:
    store = MemoryStore()
    with pytest.raises(ResumableSessionError) as error:
        await ResumableSession.resume(store, "missing")
    assert error.value.reason == "missing"
    assert store.records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("not-json synthetic-private-state-detail", "malformed"),
        ("[]", "malformed"),
        ('{"version":5}', "incompatible"),
        ('{"version":1}', "malformed"),
        (
            json.dumps(
                {
                    "version": 1,
                    "revision": "test",
                    "status": "ready",
                    "history": [42],
                    "run_state": None,
                }
            ),
            "malformed",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "revision": "test",
                    "status": "paused",
                    "history": [],
                    "run_state": "not-json",
                }
            ),
            "malformed",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "revision": "test",
                    "status": "paused",
                    "history": [],
                    "run_state": None,
                }
            ),
            "malformed",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "revision": "test",
                    "status": "active",
                    "history": [],
                    "run_state": None,
                }
            ),
            "active",
        ),
    ],
)
async def test_resume_rejects_invalid_or_unfinished_records_without_payloads(
    value: str, reason: str
) -> None:
    store = MemoryStore()
    store.records["test"] = value
    with pytest.raises(ResumableSessionError) as error:
        await ResumableSession.resume(store, "test")
    assert error.value.reason == reason
    assert "synthetic-private" not in str(error.value)
    assert error.value.__context__ is None
    assert store.records["test"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "save"])
async def test_store_failure_does_not_retain_the_store_exception(operation: str) -> None:
    store = MemoryStore()
    store.fail_load = operation == "load"
    store.fail_save = "before" if operation == "save" else None
    with pytest.raises(ResumableSessionError) as error:
        if operation == "load":
            await ResumableSession.resume(store, "test")
        else:
            await ResumableSession.create(store)
    assert error.value.reason == "unavailable"
    assert "synthetic-private" not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.asyncio
async def test_create_confirms_its_exact_record_after_a_lost_write_acknowledgment() -> None:
    store = MemoryStore()
    store.fail_save = "after"
    session = await ResumableSession.create(store)
    assert session.session_id in store.records
    assert (
        await ResumableSession.resume(store, session.session_id)
    ).session_id == session.session_id


@pytest.mark.asyncio
async def test_cancellation_waits_for_a_started_write_to_settle() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    class DelayedStore(MemoryStore):
        async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
            started.set()
            await finish.wait()
            return await super().save(session_id, value, expected=expected)

    store = DelayedStore()
    task = asyncio.create_task(ResumableSession.create(store))
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(store.records) == 1
        (session_id,) = store.records
        assert (await ResumableSession.resume(store, session_id)).session_id == session_id
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resume_preserves_provider_metadata_without_union_coercion() -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    record = json.loads(store.records[session.session_id])
    history = [
        {
            "role": "user",
            "content": "original",
            "provider_data": {"vendor_counter": 7, "extra": {"retained": True}},
        }
    ]
    record["history"] = history
    store.records[session.session_id] = json.dumps(record)
    resumed = await ResumableSession.resume(store, session.session_id)
    assert not resumed.is_paused
    assert resumed._get_history() == history
    assert json.loads(store.records[session.session_id])["history"] == history


def _sdk_traceback_locals(error: BaseException) -> str:
    frames = []
    tb = error.__traceback__
    while tb is not None:
        if "/src/agents/" in tb.tb_frame.f_code.co_filename.replace("\\", "/"):
            frames.append(repr(tb.tb_frame.f_locals))
        tb = tb.tb_next
    return "\n".join(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "resume", "load_state"])
async def test_public_checkpoint_errors_drop_sensitive_sdk_traceback_locals(operation: str) -> None:
    from agents import Agent
    from agents.run_state import CURRENT_SCHEMA_VERSION

    marker = "synthetic-checkpoint-private-value"

    class SensitiveStore(MemoryStore):
        def __repr__(self) -> str:
            return marker

    store = SensitiveStore()
    with pytest.raises(ResumableSessionError) as error:
        if operation == "create":
            store.fail_save = "before"
            await ResumableSession.create(store)
        elif operation == "resume":
            store.records["sensitive"] = marker
            await ResumableSession.resume(store, "sensitive")
        else:
            store.records["sensitive"] = json.dumps(
                {
                    "version": 1,
                    "revision": "test",
                    "status": "paused",
                    "history": [{"role": "user", "content": marker}],
                    "run_state": json.dumps({"$schemaVersion": CURRENT_SCHEMA_VERSION}),
                }
            )
            session = await ResumableSession.resume(store, "sensitive")
            await session.load_state(
                Agent(name="test", instructions=marker), context_override={"private": marker}
            )
    assert marker not in _sdk_traceback_locals(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
