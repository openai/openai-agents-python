"""Fenced canonical snapshots backed by a Dapr Redis state component."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass as dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._lifecycle import _join
from ._public import preserve_module_identity as _preserve_module_identity
from ._storage_driver import _StorageDriver

if TYPE_CHECKING:
    from _thread import LockType

    from dapr.aio.clients import DaprClient


@_sdk_dataclass(frozen=True)
class _Record:
    owner: str | None
    lease_until: float
    revision: int
    write_id: str | None
    payload: str | None


@_sdk_dataclass
class _Lease:
    token: str
    stop: threading.Event
    lock: LockType = field(default_factory=threading.Lock)
    valid: bool = True
    thread: threading.Thread | None = None


class DaprSessionStore:
    """Store native snapshots through a persistent Dapr Redis state component.

    Construct on the async client's running loop and keep that loop active
    through Session cleanup. The application owns a supplied client. Workers
    sharing a component must share its key namespace and keep clocks
    synchronized. Writer leases expire automatically after a process stops.

    The Redis component's first-write ETag compare fences the whole record.
    Keep the component configured without TTL or eviction on these keys.
    Redis persistence and replication determine server-loss durability.
    """

    def __init__(
        self,
        client: DaprClient,
        *,
        state_store_name: str,
        key_prefix: str = "agents:canonical:v1",
        lease_seconds: float = 30,
    ) -> None:
        from dapr.aio.clients import DaprClient
        from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions

        if not isinstance(client, DaprClient):
            raise TypeError("client must be an async DaprClient")
        if not isinstance(state_store_name, str) or not state_store_name:
            raise ValueError("state_store_name must be nonempty text")
        if not isinstance(key_prefix, str) or not key_prefix:
            raise ValueError("key_prefix must be nonempty text")
        if (
            not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds < 1
        ):
            raise ValueError("lease_seconds must be at least one finite second")
        self.client = client
        self.state_store_name = state_store_name
        self.key_prefix = key_prefix
        self.lease_seconds = lease_seconds
        self._options = StateOptions(
            concurrency=Concurrency.first_write, consistency=Consistency.strong
        )
        self._driver = _StorageDriver(asynchronous=True, timeout=lease_seconds)
        self._pid = os.getpid()
        self._mutex = threading.Lock()
        self._leases: dict[str, _Lease] = {}
        self._owns_client = False
        self._closed = False

    @classmethod
    async def from_address(
        cls,
        dapr_address: str = "localhost:50001",
        *,
        state_store_name: str,
        key_prefix: str = "agents:canonical:v1",
        lease_seconds: float = 30,
    ) -> DaprSessionStore:
        """Create an owned async client for the selected Dapr sidecar."""
        from dapr.aio.clients import DaprClient

        client = DaprClient(address=dapr_address)
        try:
            store = cls(
                client,
                state_store_name=state_store_name,
                key_prefix=key_prefix,
                lease_seconds=lease_seconds,
            )
        except BaseException:
            await client.close()
            raise
        store._owns_client = True
        return store

    def _check_process(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError("DaprSessionStore must be recreated after fork")
        self._driver.require_worker()

    def _key(self, session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}:{digest}"

    def _read_record(self, session_id: str) -> tuple[_Record, str]:
        response = self._driver.call(
            self.client.get_state,
            self.state_store_name,
            self._key(session_id),
            state_metadata={"consistency": "strong"},
        )
        if not response.data and not response.etag:
            # The Redis component assigns ETag 1 on create and increments it
            # on every replacement. ETag 0 can only match an absent key.
            return _Record(None, 0, 0, None, None), "0"
        record: _Record | None
        try:
            record = _Record(**json.loads(response.data))
            if (
                not response.etag
                or response.etag == "0"
                or type(record.lease_until) not in (int, float)
                or not math.isfinite(record.lease_until)
                or type(record.revision) is not int
                or record.revision < 0
            ):
                raise ValueError
            if record.owner is None:
                if record.lease_until != 0:
                    raise ValueError
            elif not isinstance(record.owner, str) or not record.owner or record.lease_until <= 0:
                raise ValueError
            if record.revision == 0:
                if record.write_id is not None or record.payload is not None:
                    raise ValueError
            elif (
                not isinstance(record.write_id, str)
                or not record.write_id
                or not isinstance(record.payload, str)
                or not record.payload
            ):
                raise ValueError
            return record, response.etag
        except (TypeError, ValueError, OverflowError):
            pass
        response = record = None
        raise RuntimeError("Invalid canonical Dapr storage record")

    def _replace_record(self, session_id: str, etag: str, record: _Record) -> bool:
        from dapr.clients.exceptions import DaprInternalError
        from grpc import StatusCode  # type: ignore[import-untyped]
        from grpc.aio import AioRpcError  # type: ignore[import-untyped]

        try:
            self._driver.call(
                self.client.save_state,
                self.state_store_name,
                self._key(session_id),
                json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":")),
                etag=etag,
                options=self._options,
            )
        except (DaprInternalError, AioRpcError) as error:
            transport_error = error.__cause__ if isinstance(error, DaprInternalError) else error
            if (
                isinstance(transport_error, AioRpcError)
                and transport_error.code() == StatusCode.ABORTED
            ):
                return False
            raise
        return True

    def _get_lease(self, session_id: str, token: str) -> _Lease | None:
        with self._mutex:
            lease = self._leases.get(session_id)
        return lease if lease is not None and lease.token == token else None

    def _read_owned(self, session_id: str, lease: _Lease) -> tuple[_Record, str]:
        if not lease.valid:
            raise RuntimeError("Canonical storage writer was fenced")
        record, etag = self._read_record(session_id)
        if record.owner != lease.token or record.lease_until <= time.time():
            lease.valid = False
            raise RuntimeError("Canonical storage writer was fenced")
        return record, etag

    def _renew(self, session_id: str, lease: _Lease) -> None:
        while not lease.stop.wait(self.lease_seconds / 3):
            with lease.lock:
                if lease.stop.is_set():
                    return
                try:
                    record, etag = self._read_owned(session_id, lease)
                    renewed = replace(record, lease_until=time.time() + self.lease_seconds)
                    if self._replace_record(session_id, etag, renewed):
                        continue
                except Exception:
                    pass
                lease.valid = False
                return

    def acquire(self, session_id: str) -> str:
        self._check_process()
        lease = _Lease(uuid.uuid4().hex, threading.Event())
        with lease.lock:
            with self._mutex:
                if self._closed:
                    raise RuntimeError("DaprSessionStore is closed")
                if session_id in self._leases:
                    raise RuntimeError("Canonical storage is already owned")
                self._leases[session_id] = lease
            try:
                record, etag = self._read_record(session_id)
                now = time.time()
                if record.owner is not None and record.lease_until > now:
                    raise RuntimeError("Canonical storage is already owned")
                owned = replace(record, owner=lease.token, lease_until=now + self.lease_seconds)
                if not self._replace_record(session_id, etag, owned):
                    raise RuntimeError("Canonical storage is already owned")
                lease.thread = threading.Thread(
                    target=self._renew, args=(session_id, lease), daemon=True
                )
                lease.thread.start()
            except BaseException:
                lease.valid = False
                lease.stop.set()
                with self._mutex:
                    del self._leases[session_id]
                raise
        return lease.token

    def read(self, session_id: str, token: str) -> str | None:
        self._check_process()
        lease = self._get_lease(session_id, token)
        if lease is None:
            raise RuntimeError("Canonical storage writer was fenced")
        with lease.lock:
            return self._read_owned(session_id, lease)[0].payload

    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        self._check_process()
        document: Any = json.loads(payload)
        if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
            raise RuntimeError("Invalid canonical revision")
        lease = self._get_lease(session_id, token)
        if lease is None:
            raise RuntimeError("Canonical storage writer was fenced")
        with lease.lock:
            record, etag = self._read_owned(session_id, lease)
            if record.write_id == write_id:
                if record.payload == payload:
                    return
                raise RuntimeError("Canonical storage write ID has been reused")
            if record.revision != expected_revision:
                raise RuntimeError("Canonical storage revision conflict")
            updated = replace(
                record, revision=expected_revision + 1, write_id=write_id, payload=payload
            )
            if not self._replace_record(session_id, etag, updated):
                raise RuntimeError("Canonical storage writer was fenced")

    def flush(self, session_id: str, token: str) -> None:
        self.read(session_id, token)

    def release(self, session_id: str, token: str) -> None:
        if os.getpid() != self._pid:
            return
        self._driver.require_worker()
        lease = self._get_lease(session_id, token)
        if lease is None:
            return
        try:
            with lease.lock:
                with self._mutex:
                    if self._leases.get(session_id) is not lease:
                        return
                lease.valid = False
                lease.stop.set()
                record, etag = self._read_record(session_id)
                if record.owner == token:
                    released = replace(record, owner=None, lease_until=0)
                    if not self._replace_record(session_id, etag, released):
                        if self._read_record(session_id)[0].owner == token:
                            raise RuntimeError("Canonical storage release was not confirmed")
                with self._mutex:
                    del self._leases[session_id]
        finally:
            if lease.thread is not None:
                lease.thread.join()

    def _close(self) -> None:
        self._check_process()
        with self._mutex:
            if self._closed:
                return
            if self._leases:
                raise RuntimeError("Close Sessions before closing DaprSessionStore")
            if self._owns_client:
                self._driver.call(self.client.close)
            self._closed = True

    async def aclose(self) -> None:
        """Close an owned client after all canonical Sessions have released it."""
        await _join(asyncio.create_task(asyncio.to_thread(self._close)))


_preserve_module_identity(globals(), "agents.canonical_storage")
