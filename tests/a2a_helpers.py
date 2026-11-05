"""Synthetic host storage/authentication for real official A2A integration tests.

This deliberately small test host is not an SDK production storage implementation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from a2a.auth.user import User
from a2a.server.agent_execution import RequestContext
from a2a.server.cluster.task_store import ConcurrentTaskModificationError, StoredTask
from a2a.server.cluster.version import TaskVersion
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue_v2 import EventQueueSource
from a2a.server.tasks import TaskManager
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from agents.a2a import NativeA2ABinding, NativeA2AStore, NativeA2ATask


@dataclass
class Principal(User):
    name: str

    @property
    def user_name(self):
        return self.name

    @property
    def is_authenticated(self):
        return bool(self.name)


def call_context(principal="alice"):
    return ServerCallContext(user=Principal(principal))


def request_context(
    task_id="task-a", context_id="context-a", principal="alice", reply=None, current=None
):
    part = p.Part(text="Do this task") if reply is None else p.Part(data=ParseDict(reply, Value()))
    return RequestContext(
        call_context=call_context(principal),
        task_id=task_id,
        context_id=context_id,
        task=current,
        request=p.SendMessageRequest(
            message=p.Message(
                message_id=os.urandom(8).hex(),
                role=p.ROLE_USER,
                task_id=task_id,
                context_id=context_id,
                parts=[part],
            )
        ),
    )


class HostStore(NativeA2AStore):
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(exist_ok=True)
        self.path = directory / "host.sqlite3"
        self.lose_ack = False
        self.fail_publication = False
        self.saved = []
        self.lock = asyncio.Lock()
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS private (owner TEXT, id TEXT, payload TEXT, "
                "ack INTEGER DEFAULT 0, PRIMARY KEY(owner,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS public (owner TEXT, id TEXT, context TEXT, "
                "version INTEGER, snapshot BLOB, PRIMARY KEY(owner,id))"
            )

    def db(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def owner(context):
        if context.tenant or not context.user.is_authenticated or not context.user.user_name:
            raise PermissionError("Authenticated host owner required")
        return context.user.user_name

    def read(self, binding):
        with self.db() as db:
            row = db.execute(
                "SELECT payload FROM private WHERE owner=? AND id=?",
                (binding.principal, binding.task_id),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["binding"] = NativeA2ABinding(**data["binding"])
        data["snapshot"] = base64.b64decode(data["snapshot"])
        record = NativeA2ATask(**data)
        if record.binding != binding:
            raise PermissionError("Context does not match current task")
        return record

    @asynccontextmanager
    async def own(self, binding):
        descriptor = None
        if os.name != "nt":
            import fcntl

            name = hashlib.sha256((binding.principal + ":" + binding.task_id).encode()).hexdigest()
            descriptor = os.open(self.directory / (name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise RuntimeError("Task owner is busy") from None
        else:
            await self.lock.acquire()
        store = self

        class Lease:
            active = True

            async def load(self):
                assert self.active
                return store.read(binding)

            async def commit(self, record, *, expected_generation):
                assert self.active and record.binding == binding
                with store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = store.read(binding)
                    assert (current.generation if current else None) == expected_generation
                    data = asdict(record)
                    data["snapshot"] = base64.b64encode(record.snapshot).decode()
                    db.execute(
                        "INSERT INTO private(owner,id,payload) VALUES(?,?,?) "
                        "ON CONFLICT(owner,id) DO UPDATE SET payload=excluded.payload",
                        (binding.principal, binding.task_id, json.dumps(data)),
                    )

        lease = Lease()
        try:
            yield lease
        finally:
            lease.active = False
            if descriptor is not None:
                os.close(descriptor)
            else:
                self.lock.release()

    async def get(self, task_id, context):
        owner = self.owner(context)
        with self.db() as db:
            row = db.execute(
                "SELECT version,snapshot FROM public WHERE owner=? AND id=?", (owner, task_id)
            ).fetchone()
        return StoredTask(p.Task.FromString(row[1]), TaskVersion(row[0])) if row else None

    async def save(self, task, *, event, prev, prev_version, context):
        owner = self.owner(context)
        binding = NativeA2ABinding(principal=owner, task_id=task.id, context_id=task.context_id)
        snapshot = task.SerializeToString(deterministic=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.read(binding)
            if (
                self.fail_publication
                and record is not None
                and record.status in {"waiting", "completed"}
            ):
                raise RuntimeError("Injected publication failure")
            row = db.execute(
                "SELECT version,snapshot FROM public WHERE owner=? AND id=?", (owner, task.id)
            ).fetchone()
            current_version = TaskVersion(row[0]) if row else TaskVersion.MISSING
            terminal = {
                p.TASK_STATE_COMPLETED,
                p.TASK_STATE_FAILED,
                p.TASK_STATE_CANCELED,
                p.TASK_STATE_REJECTED,
            }
            if row and p.Task.FromString(row[1]).status.state in terminal and row[1] != snapshot:
                raise ConcurrentTaskModificationError(task.id)
            if current_version != prev_version:
                raise ConcurrentTaskModificationError(task.id)
            if record is not None and not record.acknowledges(task):
                if record.status == "waiting" and task.status.state == p.TASK_STATE_CANCELED:
                    # Official cancellation can bypass an inactive executor.
                    record = replace(
                        record,
                        generation=record.generation + 1,
                        publication_revision=record.publication_revision + 1,
                        status="canceled",
                        checkpoint=None,
                        challenge=None,
                        snapshot=snapshot,
                    )
                    payload = asdict(record)
                    payload["snapshot"] = base64.b64encode(snapshot).decode()
                    db.execute(
                        "UPDATE private SET payload=? WHERE owner=? AND id=?",
                        (json.dumps(payload), owner, task.id),
                    )
                elif task.status.state in terminal or (
                    record.status == "waiting" and task.status.state == p.TASK_STATE_INPUT_REQUIRED
                ):
                    # A failed follow-up must not erase its current challenge.
                    raise ConcurrentTaskModificationError(task.id)
            version = (row[0] if row else 0) + 1
            db.execute(
                "INSERT INTO public VALUES(?,?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET "
                "version=excluded.version,snapshot=excluded.snapshot",
                (owner, task.id, task.context_id, version, snapshot),
            )
        self.saved.append(snapshot)
        if record is not None and record.acknowledges(task):
            if self.lose_ack and record.status in {"waiting", "completed"}:
                raise RuntimeError("Injected lost acknowledgment")
            with self.db() as db:
                db.execute(
                    "UPDATE private SET ack=? WHERE owner=? AND id=? AND ack<?",
                    (record.publication_revision, owner, task.id, record.publication_revision),
                )
        return TaskVersion(version)

    async def list(self, params, context):
        owner = self.owner(context)
        with self.db() as db:
            rows = db.execute("SELECT snapshot FROM public WHERE owner=?", (owner,)).fetchall()
        return p.ListTasksResponse(tasks=[p.Task.FromString(row[0]) for row in rows])

    async def delete(self, task_id, context):
        owner = self.owner(context)
        with self.db() as db:
            db.execute("DELETE FROM public WHERE owner=? AND id=?", (owner, task_id))

    def acknowledged(self, binding):
        with self.db() as db:
            return db.execute(
                "SELECT ack FROM private WHERE owner=? AND id=?",
                (binding.principal, binding.task_id),
            ).fetchone()[0]


async def invoke(executor, context):
    queue = EventQueueSource()
    manager = TaskManager(
        executor.store, context.call_context, context.task_id, context.context_id, context.message
    )
    events = []
    pending_message = context.message

    async def drain():
        nonlocal pending_message
        while True:
            event = await queue.dequeue_event()
            events.append(event)
            if not isinstance(event, p.Task) and pending_message is not None:
                existing = await manager.get_task()
                if existing and not any(
                    message.message_id == pending_message.message_id for message in existing.history
                ):
                    await manager.save_task_event(
                        manager.update_with_message(pending_message, existing)
                    )
                pending_message = None
            if isinstance(event, p.Task):
                pending_message = None
            await manager.process(event)
            queue.task_done()

    reader = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(executor.execute(context, queue), 20)
        await queue.test_only_join_incoming_queue()
        await asyncio.sleep(0.02)
        if reader.done():
            reader.result()
        return await executor.store.get(context.task_id, context.call_context), events
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await queue.close(immediate=True)
