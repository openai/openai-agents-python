"""Lease-fenced MongoDB storage for complete native canonical snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from ._legacy_history import (
    check_legacy_history_size,
    decode_legacy_item,
    validate_legacy_history_limits,
)
from ._lifecycle import _join
from ._public import preserve_module_identity as _preserve_module_identity
from ._storage_driver import _StorageDriver


@dataclass
class _Lease:
    token: str
    pid: int
    stop: threading.Event
    valid: bool = True
    thread: threading.Thread | None = None


class MongoDBSessionStore:
    """Store one canonical snapshot per MongoDB document using server-time leases.

    Supply a MongoClient or AsyncMongoClient and a database/collection. The adapter uses
    primary majority reads and majority journaled writes. MongoDB 5.0 or newer
    is required for server-time lease updates. Snapshots must fit MongoDB's BSON
    document limit; Classic per-item collections are not migrated. Optional ttl
    expires whole snapshot documents after their last commit, subject to MongoDB's
    TTL monitor delay. Snapshot expiration is independent of writer renewal.

    The application owns a supplied client. Configure finite network timeouts.
    For an async client, construct the store on its event loop and keep that
    loop running through Session cleanup. Await aclose() after the Sessions
    have closed. Create a new adapter after fork. Every operation fences against both token
    and server-side lease expiry; a failed renewal invalidates the local writer.
    """

    def __init__(
        self,
        client: Any,
        *,
        database: str,
        collection: str = "agents_canonical_threads",
        lease_seconds: float = 30,
        ttl: int | None = None,
    ) -> None:
        import pymongo
        from pymongo.read_concern import ReadConcern
        from pymongo.read_preferences import ReadPreference
        from pymongo.write_concern import WriteConcern

        asynchronous = not isinstance(client, pymongo.MongoClient)
        async_client = getattr(pymongo, "AsyncMongoClient", None)
        if asynchronous and (async_client is None or not isinstance(client, async_client)):
            raise TypeError("client must be a MongoClient or AsyncMongoClient")
        if not isinstance(database, str) or not database:
            raise ValueError("database must be nonempty text")
        if not isinstance(collection, str) or not collection:
            raise ValueError("collection must be nonempty text")
        if not isinstance(lease_seconds, (int, float)) or lease_seconds < 1:
            raise ValueError("lease_seconds must be at least one second")
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1):
            raise ValueError("ttl must be a positive integer or None")
        self.client = client
        self.collection = client[database][collection].with_options(
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern(w="majority", j=True),
            read_preference=ReadPreference.PRIMARY,
        )
        self.lease_milliseconds = int(lease_seconds * 1000)
        self.ttl = ttl
        self._driver = _StorageDriver(asynchronous=asynchronous, timeout=lease_seconds)
        self._pid = os.getpid()
        self._closed = False
        self._owns_client = False
        self._mutex = threading.RLock()
        self._owners: dict[str, _Lease] = {}
        self._ttl_index_pending = ttl is not None and asynchronous
        if ttl is not None and not asynchronous:
            self.collection.create_index("expires_at", expireAfterSeconds=0)

    @classmethod
    def from_uri(
        cls,
        uri: str,
        *,
        database: str,
        collection: str = "agents_canonical_threads",
        lease_seconds: float = 30,
        ttl: int | None = None,
        **client_options: Any,
    ) -> MongoDBSessionStore:
        """Create an owned synchronous client with bounded network timeouts."""
        from pymongo import MongoClient

        client_options.setdefault("serverSelectionTimeoutMS", 5000)
        client_options.setdefault("socketTimeoutMS", 5000)
        client_options.setdefault("connectTimeoutMS", 5000)
        client: Any = MongoClient(uri, **client_options)
        try:
            store = cls(
                client,
                database=database,
                collection=collection,
                lease_seconds=lease_seconds,
                ttl=ttl,
            )
        except BaseException:
            client.close()
            raise
        store._owns_client = True
        return store

    def _check_process(self) -> None:
        if self._closed:
            raise RuntimeError("MongoDBSessionStore is closed")
        if self._pid != os.getpid():
            raise RuntimeError("MongoDBSessionStore cannot be reused after fork")
        self._driver.require_worker()

    def _owner(self, session_id: str, token: str) -> _Lease:
        self._check_process()
        owner = self._owners.get(session_id)
        if owner is None or owner.token != token or not owner.valid:
            raise RuntimeError("Canonical storage writer is stale or closed")
        return owner

    def _deadline(self) -> dict[str, Any]:
        return {
            "$dateAdd": {
                "startDate": "$$NOW",
                "unit": "millisecond",
                "amount": self.lease_milliseconds,
            }
        }

    @staticmethod
    def _filter(session_id: str, token: str) -> dict[str, Any]:
        return {"_id": session_id, "owner": token, "$expr": {"$gt": ["$lease_until", "$$NOW"]}}

    def acquire(self, session_id: str) -> str:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        self._check_process()
        with self._mutex:
            self._check_process()
            if session_id in self._owners:
                raise RuntimeError("Canonical storage already has a writer")
            if self._ttl_index_pending:
                self._driver.call(self.collection.create_index, "expires_at", expireAfterSeconds=0)
                self._ttl_index_pending = False
            # Bootstrap is insert-only; a competing creator cannot replace data.
            try:
                self._driver.call(
                    self.collection.update_one,
                    {"_id": session_id},
                    {
                        "$setOnInsert": {
                            "owner": None,
                            "revision": 0,
                            "write_id": None,
                            "payload": None,
                        }
                    },
                    upsert=True,
                )
            except DuplicateKeyError:
                pass
            token = uuid.uuid4().hex
            claimed = self._driver.call(
                self.collection.find_one_and_update,
                {
                    "_id": session_id,
                    "$or": [
                        {"owner": None},
                        {"$expr": {"$lte": ["$lease_until", "$$NOW"]}},
                    ],
                },
                [{"$set": {"owner": {"$literal": token}, "lease_until": self._deadline()}}],
                return_document=ReturnDocument.AFTER,
            )
            if claimed is None:
                raise RuntimeError("Canonical storage already has a writer")
            owner = _Lease(token, os.getpid(), threading.Event())

            def renew() -> None:
                while not owner.stop.wait(self.lease_milliseconds / 3000):
                    try:
                        result = self._driver.call(
                            self.collection.update_one,
                            self._filter(session_id, token),
                            [{"$set": {"lease_until": self._deadline()}}],
                        )
                        if result.matched_count != 1:
                            owner.valid = False
                            return
                    except Exception:
                        owner.valid = False
                        return

            owner.thread = threading.Thread(target=renew, daemon=True, name="canonical-mongo-lease")
            self._owners[session_id] = owner
            try:
                owner.thread.start()
            except BaseException:
                self._owners.pop(session_id)
                self._driver.call(
                    self.collection.update_one,
                    {"_id": session_id, "owner": token},
                    {"$set": {"owner": None}},
                )
                raise
            return token

    def read(self, session_id: str, token: str) -> str | None:
        self._check_process()
        with self._mutex:
            self._owner(session_id, token)
            document = self._driver.call(self.collection.find_one, self._filter(session_id, token))
            if document is None:
                raise RuntimeError("Canonical storage writer was fenced")
            value: str | None = document["payload"]
            return value

    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        self._check_process()
        document = json.loads(payload)
        if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
            raise RuntimeError("Invalid canonical revision")
        with self._mutex:
            self._owner(session_id, token)
            current = self._driver.call(self.collection.find_one, self._filter(session_id, token))
            if current is None:
                raise RuntimeError("Canonical storage writer was fenced")
            same = current["write_id"] == write_id and current["payload"] == payload
            if not same and current["revision"] != expected_revision:
                raise RuntimeError("Canonical storage revision conflict")
            condition = self._filter(session_id, token)
            condition["revision"] = current["revision"] if same else expected_revision
            values: dict[str, Any] = {
                "revision": expected_revision + 1,
                "write_id": {"$literal": write_id},
                "payload": {"$literal": payload},
            }
            if self.ttl is not None:
                values["expires_at"] = {
                    "$dateAdd": {
                        "startDate": "$$NOW",
                        "unit": "second",
                        "amount": self.ttl,
                    }
                }
            pipeline: list[dict[str, Any]] = [{"$set": values}]
            if self.ttl is None:
                pipeline.append({"$unset": "expires_at"})
            result = self._driver.call(self.collection.update_one, condition, pipeline)
            if result.matched_count != 1:
                raise RuntimeError("Canonical storage revision conflict or writer was fenced")

    def flush(self, session_id: str, token: str) -> None:
        self.read(session_id, token)

    def release(self, session_id: str, token: str) -> None:
        if os.getpid() != self._pid:
            return
        self._driver.require_worker()
        with self._mutex:
            owner = self._owners.get(session_id)
            if owner is None or owner.token != token:
                return
            owner.valid = False
            owner.stop.set()
            assert owner.thread is not None
            owner.thread.join()
            self._driver.call(
                self.collection.update_one,
                {"_id": session_id, "owner": token},
                {"$set": {"owner": None}},
            )
            self._owners.pop(session_id)

    def ping(self) -> bool:
        """Check server availability without modifying canonical history."""
        self._check_process()
        return bool(self._driver.call(self.client.admin.command, "ping").get("ok"))

    def close(self) -> None:
        """Close an owned client after all Session writers have released it."""
        if os.getpid() != self._pid:
            return
        self._driver.require_worker()
        with self._mutex:
            if self._owners:
                raise RuntimeError("Close Sessions before closing MongoDBSessionStore")
            if self._owns_client:
                self.client.close()
            self._closed = True

    async def aclose(self) -> None:
        """Close the store after its Sessions have released it."""
        await _join(asyncio.create_task(asyncio.to_thread(self.close)))

    async def read_legacy_history(
        self,
        session_id: str,
        *,
        sessions_collection: str = "agent_sessions",
        messages_collection: str = "agent_messages",
        max_items: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> list[dict[str, Any]]:
        """Read the active v0 generation and its items in insertion order.

        Finish writes to the v0 session before reading. Import the items with
        the current revision of an empty native Session's `transact_history`.
        Both limits apply to individual items, including items in v0 batches.
        """
        from pymongo import ReadPreference

        if not isinstance(session_id, str):
            raise TypeError("session_id must be text")
        validate_legacy_history_limits(max_items, max_bytes)
        database = self.collection.database
        sessions = database[sessions_collection].with_options(
            read_preference=ReadPreference.PRIMARY
        )
        messages = database[messages_collection].with_options(
            read_preference=ReadPreference.PRIMARY
        )

        def read() -> list[dict[str, Any]]:
            self._check_process()
            with self._mutex:
                self._check_process()
                selector: dict[str, Any] = {"session_id": session_id}
                head = self._driver.call(sessions.find_one, selector, {"_generation": 1})
                generation = head.get("_generation") if head is not None else None
                selector["generation"] = (
                    generation if isinstance(generation, int) and generation else {"$in": [0, None]}
                )

                # A v0 pop can leave an empty batch after claiming its last item.
                selector["message_data"] = {"$not": {"$size": 0}}
                cursor = (
                    messages.find(selector, {"message_data": 1})
                    .sort("seq", 1)
                    .limit(max_items + 1)
                    .batch_size(1)
                )
                items: list[dict[str, Any]] = []
                encoded_bytes = 0
                try:
                    while rows := self._driver.call(cursor.to_list, length=1):
                        batch = rows[0]["message_data"]
                        records = batch if isinstance(batch, list) else [batch]
                        count = len(items) + len(records)
                        check_legacy_history_size(count, encoded_bytes, max_items, max_bytes)
                        for item in records:
                            if not isinstance(item, str):
                                raise ValueError("A v0 MongoDB session item must be JSON text")
                            encoded_bytes += len(item.encode("utf-8"))
                            check_legacy_history_size(count, encoded_bytes, max_items, max_bytes)
                            items.append(decode_legacy_item(item))
                finally:
                    self._driver.call(cursor.close)
                return items

        items: list[dict[str, Any]] = await _join(asyncio.create_task(asyncio.to_thread(read)))
        return items


_preserve_module_identity(globals(), "agents.canonical_storage")
