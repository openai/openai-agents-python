"""Focused offline tests for the Azure Cosmos DB session backend."""

from __future__ import annotations

import asyncio
import copy
import sys
import types
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from agents import TResponseInputItem

pytestmark = pytest.mark.asyncio


class FakeCosmosHttpResponseError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"Cosmos request failed with status {status_code}")
        self.status_code = status_code
        self.headers = headers or {}


class FakeCosmosBatchOperationError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"Cosmos batch failed with status {status_code}")
        self.status_code = status_code
        self.headers = headers or {}


class FakeServiceRequestError(RuntimeError):
    pass


class FakeServiceResponseError(RuntimeError):
    pass


class FakeCosmosClientTimeoutError(RuntimeError):
    pass


class FakeAsyncIterator:
    def __init__(self, values: list[Any], error: BaseException | None = None) -> None:
        self._values = values
        self._error = error

    async def __aiter__(self) -> AsyncIterator[Any]:
        if self._error is not None:
            raise self._error
        for value in self._values:
            yield copy.deepcopy(value)


class FakeContainerProxy:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.patch_calls = 0
        self.patch_attempts = 0
        self.batch_calls: list[list[tuple[str, tuple[Any, ...]]]] = []
        self.batch_attempts = 0
        self.delete_attempts: list[str] = []
        self.fail_patch_after_apply: BaseException | None = None
        self.fail_delete_before_commit: BaseException | None = None
        self.fail_batch_before_commit: BaseException | None = None
        self.fail_batch_after_commit: BaseException | None = None
        self.mutate_after_commit: tuple[str, Any] | None = None
        self.read_calls = 0
        self.read_error: BaseException | None = None
        self.query_error: BaseException | None = None
        self.properties: dict[str, Any] = {
            "partitionKey": {"paths": ["/sessionId"]},
            "defaultTtl": -1,
            "indexingPolicy": {
                "indexingMode": "consistent",
                "includedPaths": [{"path": "/*"}],
            },
        }

    async def read(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.read_calls += 1
        if self.read_error is not None:
            raise self.read_error
        return copy.deepcopy(self.properties)

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        document_id = body["id"]
        if document_id in self.documents:
            raise FakeCosmosHttpResponseError(409)
        stored = copy.deepcopy(body)
        stored["_etag"] = f"etag-{document_id}"
        self.documents[document_id] = stored
        return copy.deepcopy(stored)

    async def patch_item(
        self,
        item: str,
        partition_key: str,
        patch_operations: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        del partition_key
        etag = kwargs.pop("etag", None)
        del kwargs
        self.patch_attempts += 1
        if item not in self.documents:
            raise FakeCosmosHttpResponseError(404)
        if etag is not None and self.documents[item].get("_etag") != etag:
            raise FakeCosmosHttpResponseError(412)
        self.patch_calls += 1
        document = self.documents[item]
        for operation in patch_operations:
            field = operation["path"].removeprefix("/")
            if operation["op"] == "incr":
                document[field] += operation["value"]
            else:
                document[field] = operation["value"]
        document["_etag"] = f"etag-{self.patch_calls}"
        if self.fail_patch_after_apply is not None:
            error = self.fail_patch_after_apply
            self.fail_patch_after_apply = None
            raise error
        return copy.deepcopy(document)

    async def read_item(self, item: str, partition_key: str, **kwargs: Any) -> dict[str, Any]:
        del partition_key, kwargs
        try:
            return copy.deepcopy(self.documents[item])
        except KeyError:
            raise FakeCosmosHttpResponseError(404) from None

    async def delete_item(
        self,
        item: str,
        partition_key: str,
        etag: str | None = None,
        **kwargs: Any,
    ) -> None:
        del partition_key, kwargs
        self.delete_attempts.append(item)
        if self.fail_delete_before_commit is not None:
            error = self.fail_delete_before_commit
            self.fail_delete_before_commit = None
            raise error
        if item not in self.documents:
            raise FakeCosmosHttpResponseError(404)
        if etag is not None and self.documents[item].get("_etag") != etag:
            raise FakeCosmosHttpResponseError(412)
        del self.documents[item]

    async def execute_item_batch(
        self,
        batch_operations: list[tuple[str, tuple[Any, ...]]],
        partition_key: str,
        **kwargs: Any,
    ) -> list[dict[str, int]]:
        del kwargs
        self.batch_attempts += 1
        if self.fail_batch_before_commit is not None:
            error = self.fail_batch_before_commit
            self.fail_batch_before_commit = None
            raise error
        candidate = copy.deepcopy(self.documents)
        created_ids: list[str] = []
        for operation, arguments in batch_operations:
            if operation == "create":
                document = copy.deepcopy(arguments[0])
                assert document["sessionId"] == partition_key
                if document["id"] in candidate:
                    raise FakeCosmosBatchOperationError(409)
                document["_etag"] = f"etag-{document['id']}"
                candidate[document["id"]] = document
                created_ids.append(document["id"])
            else:
                assert operation == "delete"
                document_id = arguments[0]
                if document_id not in candidate:
                    raise FakeCosmosBatchOperationError(404)
                del candidate[document_id]
        self.batch_calls.append(copy.deepcopy(batch_operations))
        self.documents = candidate
        if self.mutate_after_commit is not None and created_ids:
            field, value = self.mutate_after_commit
            self.documents[created_ids[0]][field] = value
            self.mutate_after_commit = None
        if self.fail_batch_after_commit is not None:
            error = self.fail_batch_after_commit
            self.fail_batch_after_commit = None
            raise error
        return [{"statusCode": 201} for _ in batch_operations]

    def query_items(
        self,
        query: str,
        partition_key: str,
        parameters: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> FakeAsyncIterator:
        del kwargs
        if self.query_error is not None:
            return FakeAsyncIterator([], self.query_error)
        parameter_values = {parameter["name"]: parameter["value"] for parameter in parameters or []}
        if query == "SELECT VALUE c.id FROM c":
            return FakeAsyncIterator(list(self.documents))
        if query == (
            "SELECT VALUE c.id FROM c WHERE "
            "(c.type = 'message' OR c.type = 'pop_tombstone') "
            "AND c.incarnation = @incarnation"
        ):
            return FakeAsyncIterator(
                [
                    document["id"]
                    for document in self.documents.values()
                    if document.get("sessionId") == partition_key
                    and document.get("type") in {"message", "pop_tombstone"}
                    and document.get("incarnation") == parameter_values.get("@incarnation")
                ]
            )
        if query == "SELECT c.id, c.incarnation FROM c WHERE c.type = 'message'":
            return FakeAsyncIterator(
                [
                    {"id": document["id"], "incarnation": document.get("incarnation")}
                    for document in self.documents.values()
                    if document.get("sessionId") == partition_key
                    and document.get("type") == "message"
                ]
            )
        documents = [
            document
            for document in self.documents.values()
            if document.get("sessionId") == partition_key
            and document.get("type") == "message"
            and (
                "@incarnation" not in parameter_values
                or document.get("incarnation") == parameter_values["@incarnation"]
            )
        ]
        documents.sort(key=lambda document: document["seq"], reverse="DESC" in query)
        if query.startswith("SELECT TOP 1"):
            documents = documents[:1]
        elif "@limit" in parameter_values:
            documents = documents[: parameter_values["@limit"]]
        return FakeAsyncIterator(documents)


class FakeDatabaseProxy:
    def __init__(self, container: FakeContainerProxy) -> None:
        self.container = container
        self.read_calls = 0
        self.read_error: BaseException | None = None

    async def read(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        self.read_calls += 1
        if self.read_error is not None:
            raise self.read_error
        return {"id": "agents"}

    def get_container_client(self, container: str) -> FakeContainerProxy:
        del container
        return self.container


class FakeCosmosClient:
    def __init__(self, container: FakeContainerProxy | None = None, **kwargs: Any) -> None:
        retry_write = kwargs.pop("retry_write", False)
        consistency_level = kwargs.pop("consistency_level", "Session")
        del kwargs
        self.container = container or FakeContainerProxy()
        self.database = FakeDatabaseProxy(self.container)
        self.client_connection = types.SimpleNamespace(
            connection_policy=types.SimpleNamespace(
                RetryNonIdempotentWrites=retry_write,
            ),
            default_headers={"x-ms-consistency-level": consistency_level},
        )
        self.entered = False
        self.closed = False

    @classmethod
    def from_connection_string(cls, connection_string: str, **kwargs: Any) -> FakeCosmosClient:
        del connection_string, kwargs
        return cls()

    async def __aenter__(self) -> FakeCosmosClient:
        self.entered = True
        return self

    async def close(self) -> None:
        self.closed = True

    def get_database_client(self, database: str) -> FakeDatabaseProxy:
        del database
        return self.database


class ConcurrentPopContainer(FakeContainerProxy):
    def __init__(self) -> None:
        super().__init__()
        self.pop_query_waiters = 0
        self.pop_queries_ready = asyncio.Event()
        self.release_pop_queries = asyncio.Event()
        self.pause_pop_queries = True

    def query_items(
        self,
        query: str,
        partition_key: str,
        parameters: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        documents = super().query_items(
            query,
            partition_key,
            parameters=parameters,
            **kwargs,
        )
        if "ORDER BY c.seq DESC" not in query or not self.pause_pop_queries:
            return documents

        async def iterate() -> AsyncIterator[Any]:
            values = [document async for document in documents]
            self.pop_query_waiters += 1
            if self.pop_query_waiters == 2:
                self.pause_pop_queries = False
                self.pop_queries_ready.set()
            await self.release_pop_queries.wait()
            for value in values:
                yield value

        return iterate()


class ConcurrentMetadataReadContainer(FakeContainerProxy):
    def __init__(self) -> None:
        super().__init__()
        self.pause_metadata_reads = False
        self.metadata_read_waiters = 0
        self.metadata_reads_ready = asyncio.Event()
        self.release_metadata_reads = asyncio.Event()

    async def read_item(
        self,
        item: str,
        partition_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        document = await super().read_item(item, partition_key, **kwargs)
        if item != "__session_meta__" or not self.pause_metadata_reads:
            return document

        self.metadata_read_waiters += 1
        if self.metadata_read_waiters == 2:
            self.pause_metadata_reads = False
            self.metadata_reads_ready.set()
        await self.release_metadata_reads.wait()
        return document


class PausedCleanupContainer(FakeContainerProxy):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()

    async def execute_item_batch(
        self,
        batch_operations: list[tuple[str, tuple[Any, ...]]],
        partition_key: str,
        **kwargs: Any,
    ) -> list[dict[str, int]]:
        if batch_operations and all(operation == "delete" for operation, _ in batch_operations):
            self.cleanup_started.set()
            await self.release_cleanup.wait()
        return await super().execute_item_batch(
            batch_operations,
            partition_key,
            **kwargs,
        )


class PausedCleanupQueryContainer(FakeContainerProxy):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self._paused_cleanup = False

    def query_items(
        self,
        query: str,
        partition_key: str,
        parameters: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        is_cleanup = query.startswith("SELECT c.id, c.incarnation FROM c") or query.startswith(
            "SELECT VALUE c.id FROM c WHERE (c.type = 'message' OR c.type = 'pop_tombstone')"
        )
        if not is_cleanup or self._paused_cleanup:
            return super().query_items(
                query,
                partition_key,
                parameters=parameters,
                **kwargs,
            )

        self._paused_cleanup = True

        async def iterate() -> AsyncIterator[Any]:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            results = super(PausedCleanupQueryContainer, self).query_items(
                query,
                partition_key,
                parameters=parameters,
                **kwargs,
            )
            async for result in results:
                yield result

        return iterate()


class PausedDeleteContainer(FakeContainerProxy):
    def __init__(self) -> None:
        super().__init__()
        self.delete_started = asyncio.Event()
        self.release_delete = asyncio.Event()

    async def delete_item(
        self,
        item: str,
        partition_key: str,
        etag: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.delete_started.set()
        await self.release_delete.wait()
        await super().delete_item(
            item,
            partition_key,
            etag=etag,
            **kwargs,
        )


def _install_fake_azure_modules() -> None:
    azure = types.ModuleType("azure")
    core = types.ModuleType("azure.core")
    credentials_async = types.ModuleType("azure.core.credentials_async")
    core_exceptions = types.ModuleType("azure.core.exceptions")
    cosmos = types.ModuleType("azure.cosmos")
    cosmos_aio = types.ModuleType("azure.cosmos.aio")
    cosmos_exceptions = types.ModuleType("azure.cosmos.exceptions")

    core.MatchConditions = types.SimpleNamespace(IfNotModified="IfNotModified")  # type: ignore[attr-defined]
    credentials_async.AsyncTokenCredential = object  # type: ignore[attr-defined]
    core_exceptions.ServiceRequestError = FakeServiceRequestError  # type: ignore[attr-defined]
    core_exceptions.ServiceResponseError = FakeServiceResponseError  # type: ignore[attr-defined]
    cosmos_aio.ContainerProxy = FakeContainerProxy  # type: ignore[attr-defined]
    cosmos_aio.CosmosClient = FakeCosmosClient  # type: ignore[attr-defined]
    cosmos_aio.DatabaseProxy = FakeDatabaseProxy  # type: ignore[attr-defined]
    cosmos_exceptions.CosmosBatchOperationError = FakeCosmosBatchOperationError  # type: ignore[attr-defined]
    cosmos_exceptions.CosmosClientTimeoutError = FakeCosmosClientTimeoutError  # type: ignore[attr-defined]
    cosmos_exceptions.CosmosHttpResponseError = FakeCosmosHttpResponseError  # type: ignore[attr-defined]

    sys.modules["azure"] = azure
    sys.modules["azure.core"] = core
    sys.modules["azure.core.credentials_async"] = credentials_async
    sys.modules["azure.core.exceptions"] = core_exceptions
    sys.modules["azure.cosmos"] = cosmos
    sys.modules["azure.cosmos.aio"] = cosmos_aio
    sys.modules["azure.cosmos.exceptions"] = cosmos_exceptions


_install_fake_azure_modules()

from agents.extensions.memory.cosmosdb_session import (  # noqa: E402
    ContainerValidationResult,
    CosmosDBSession,
    CosmosSessionConfigurationError,
    CosmosSessionConflictError,
)


class PausedWriteSession(CosmosDBSession):
    def __init__(self, client: FakeCosmosClient) -> None:
        super().__init__("session-1", client=client)  # type: ignore[arg-type]
        self.reserved = asyncio.Event()
        self.release_write = asyncio.Event()

    async def _write_reserved_batch_with_retry(
        self,
        documents: Sequence[dict[str, Any]],
    ) -> None:
        self.reserved.set()
        await self.release_write.wait()
        await super()._write_reserved_batch_with_retry(documents)


class PausedClearSession(CosmosDBSession):
    def __init__(self, client: FakeCosmosClient) -> None:
        super().__init__("session-1", client=client)  # type: ignore[arg-type]
        self.clear_started = asyncio.Event()
        self.release_clear = asyncio.Event()

    async def _rotate_session_incarnation(self) -> tuple[str, str | None]:
        self.clear_started.set()
        await self.release_clear.wait()
        return await super()._rotate_session_incarnation()


async def test_add_items_reserves_once_and_writes_one_atomic_batch() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    await session.add_items(items)

    assert container.patch_calls == 1
    assert len(container.batch_calls) == 1
    operations = container.batch_calls[0]
    assert [operation for operation, _ in operations] == ["create", "create"]
    documents = [arguments[0] for _, arguments in operations]
    incarnation = documents[0]["incarnation"]
    assert [document["id"] for document in documents] == [
        f"{incarnation}:000000000000",
        f"{incarnation}:000000000001",
    ]
    assert await session.get_items() == items


async def test_transient_batch_retry_reuses_serialization_and_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeContainerProxy()
    container.fail_batch_before_commit = FakeCosmosBatchOperationError(
        503,
        headers={"x-ms-retry-after-ms": "0"},
    )
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=2,
        retry_backoff_seconds=0,
    )
    serialize_calls = 0

    from agents.extensions.memory import cosmosdb_session

    serialize_item = cosmosdb_session._serialize_item

    def observe_serialization(item: TResponseInputItem) -> str:
        nonlocal serialize_calls
        serialize_calls += 1
        return serialize_item(item)

    monkeypatch.setattr(cosmosdb_session, "_serialize_item", observe_serialization)

    await session.add_items([{"role": "user", "content": "hello"}])

    assert serialize_calls == 1
    assert container.patch_calls == 1
    assert container.batch_attempts == 2
    assert len(container.batch_calls) == 1


async def test_concurrent_etag_reservations_receive_distinct_sequences() -> None:
    container = ConcurrentMetadataReadContainer()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "seed"}])
    container.pause_metadata_reads = True

    first = asyncio.create_task(session.add_items([{"role": "assistant", "content": "first"}]))
    second = asyncio.create_task(session.add_items([{"role": "assistant", "content": "second"}]))
    await container.metadata_reads_ready.wait()
    container.release_metadata_reads.set()
    await asyncio.gather(first, second)

    messages = [
        document for document in container.documents.values() if document.get("type") == "message"
    ]
    assert sorted(document["seq"] for document in messages) == [0, 1, 2]
    assert container.documents["__session_meta__"]["last_seq"] == 3
    stored_items = cast("list[dict[str, str]]", await session.get_items())
    assert sorted(item["content"] for item in stored_items) == [
        "first",
        "second",
        "seed",
    ]


async def test_ambiguous_commit_is_resolved_by_strict_point_reads() -> None:
    container = FakeContainerProxy()
    container.fail_batch_after_commit = FakeServiceResponseError("ambiguous commit")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=2,
        retry_backoff_seconds=0,
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]

    await session.add_items(items)

    assert container.patch_calls == 1
    assert container.batch_attempts == 2
    assert len(container.batch_calls) == 1
    assert await session.get_items() == items


async def test_final_ambiguous_commit_is_resolved_by_strict_point_reads() -> None:
    container = FakeContainerProxy()
    container.fail_batch_after_commit = FakeServiceResponseError("ambiguous commit")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=1,
        retry_backoff_seconds=0,
    )
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert container.batch_attempts == 1
    assert await session.get_items() == [item]


async def test_cosmos_timeout_after_commit_is_resolved_by_strict_point_reads() -> None:
    container = FakeContainerProxy()
    container.fail_batch_after_commit = FakeCosmosClientTimeoutError("ambiguous commit")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=1,
        retry_backoff_seconds=0,
    )
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert container.batch_attempts == 1
    assert await session.get_items() == [item]


async def test_final_transient_failure_without_commit_is_raised() -> None:
    container = FakeContainerProxy()
    container.fail_batch_before_commit = FakeServiceResponseError("write unavailable")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=1,
        retry_backoff_seconds=0,
    )

    with pytest.raises(FakeServiceResponseError, match="write unavailable"):
        await session.add_items([{"role": "user", "content": "not committed"}])

    assert container.documents["__session_meta__"]["last_seq"] == 1
    assert await session.get_items() == []


async def test_replayed_batch_mismatch_raises_sanitized_conflict() -> None:
    secret = "SENTINEL-STORED-CONTENT"
    container = FakeContainerProxy()
    container.mutate_after_commit = ("message_data", secret)
    container.fail_batch_after_commit = FakeServiceRequestError("ambiguous commit")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=2,
        retry_backoff_seconds=0,
    )

    with pytest.raises(CosmosSessionConflictError) as raised:
        await session.add_items([{"role": "user", "content": "expected"}])

    assert raised.value.code == "reserved_message_mismatch"
    assert raised.value.mismatched_fields == ("message_data",)
    rendered = f"{raised.value!s}\n{raised.value!r}\n{vars(raised.value)!r}"
    assert secret not in rendered
    assert not hasattr(raised.value, "expected")
    assert not hasattr(raised.value, "existing")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "wrong-id"),
        ("type", "operation"),
        ("schema_version", 1),
        ("created_at", "2000-01-01T00:00:00Z"),
        ("ttl", 60),
    ],
)
async def test_replayed_batch_requires_every_application_field(
    field: str,
    value: Any,
) -> None:
    container = FakeContainerProxy()
    container.mutate_after_commit = (field, value)
    container.fail_batch_after_commit = FakeServiceResponseError("ambiguous commit")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=2,
        retry_backoff_seconds=0,
    )

    with pytest.raises(CosmosSessionConflictError) as raised:
        await session.add_items([{"role": "user", "content": "expected"}])

    assert raised.value.code == "reserved_message_mismatch"
    assert raised.value.mismatched_fields == (field,)


async def test_ambiguous_sequence_reservation_is_not_retried() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        max_write_attempts=3,
        retry_backoff_seconds=0,
    )
    await session.add_items([{"role": "user", "content": "first"}])
    patch_attempts_before = container.patch_attempts
    container.fail_patch_after_apply = FakeCosmosHttpResponseError(503)

    with pytest.raises(FakeCosmosHttpResponseError):
        await session.add_items([{"role": "user", "content": "ambiguous"}])

    assert container.patch_attempts == patch_attempts_before + 1
    assert container.documents["__session_meta__"]["last_seq"] == 2
    assert await session.get_items() == [{"role": "user", "content": "first"}]


async def test_cancelled_append_waits_for_the_reserved_mutation_to_settle() -> None:
    client = FakeCosmosClient()
    session = PausedWriteSession(client)
    item: TResponseInputItem = {"role": "user", "content": "settled"}
    append = asyncio.create_task(session.add_items([item]))
    await session.reserved.wait()

    append.cancel()
    session.release_write.set()

    with pytest.raises(asyncio.CancelledError):
        await append
    reader = CosmosDBSession("session-1", client=client)  # type: ignore[arg-type]
    assert await reader.get_items() == [item]


async def test_separate_add_calls_are_intentionally_not_idempotent() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    item: TResponseInputItem = {"role": "user", "content": "same"}

    await session.add_items([item])
    await session.add_items([item])

    assert await session.get_items() == [item, item]
    assert container.documents["__session_meta__"]["last_seq"] == 2
    assert len(container.batch_calls) == 2


async def test_oversized_batch_is_rejected_before_side_effects() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": str(index)} for index in range(101)
    ]

    with pytest.raises(ValueError, match="at most 100"):
        await session.add_items(items)

    assert container.documents == {}
    assert container.patch_attempts == 0
    assert container.batch_attempts == 0


async def test_get_items_limit_skips_corrupt_tail_and_finds_valid_history() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        session_settings={"limit": 1},
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    await session.add_items(items)
    latest = max(
        (
            document
            for document in container.documents.values()
            if document.get("type") == "message"
        ),
        key=lambda document: document["seq"],
    )
    latest["message_data"] = "not-json"

    assert await session.get_items() == [items[1]]
    assert await session.get_items(limit=2) == items[:2]
    assert await session.get_items(limit=0) == []


async def test_pop_skips_corrupt_tail_and_removes_latest_valid_item() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    await session.add_items(items)
    latest = max(
        (
            document
            for document in container.documents.values()
            if document.get("type") == "message"
        ),
        key=lambda document: document["seq"],
    )
    latest["message_data"] = "not-json"

    assert await session.pop_item() == items[1]
    assert latest["id"] in container.documents
    assert await session.pop_item() == items[0]
    assert await session.pop_item() is None


async def test_clear_rotates_fence_and_new_appends_use_new_incarnation() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "old"}])
    old_incarnation = container.documents["__session_meta__"]["incarnation"]

    await session.clear_session()

    metadata = container.documents["__session_meta__"]
    assert metadata["incarnation"] != old_incarnation
    assert metadata["last_seq"] == 0
    assert await session.get_items() == []

    new_item: TResponseInputItem = {"role": "assistant", "content": "new"}
    await session.add_items([new_item])
    message = next(
        document for document in container.documents.values() if document.get("type") == "message"
    )
    assert message["incarnation"] == metadata["incarnation"]
    assert message["seq"] == 0
    assert await session.get_items() == [new_item]


async def test_clear_cleanup_failure_does_not_reverse_committed_fence() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "old"}])
    container.fail_batch_before_commit = FakeServiceResponseError("cleanup unavailable")

    await session.clear_session()

    assert await session.get_items() == []
    assert any(document.get("type") == "message" for document in container.documents.values())


async def test_invalid_metadata_fails_closed() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "hello"}])
    container.documents["__session_meta__"]["schema_version"] = 1

    with pytest.raises(CosmosSessionConflictError) as raised:
        await session.get_items()

    assert raised.value.code == "invalid_session_metadata"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sessionId", "other-session"),
        ("type", "operation"),
        ("schema_version", 1),
        ("last_seq", -1),
    ],
)
@pytest.mark.parametrize("operation", ["add", "clear"])
async def test_mutations_do_not_rewrite_invalid_metadata(
    operation: str,
    field: str,
    value: Any,
) -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "existing"}])
    container.documents["__session_meta__"][field] = value
    before = copy.deepcopy(container.documents)

    with pytest.raises(CosmosSessionConflictError):
        if operation == "add":
            await session.add_items([{"role": "user", "content": "new"}])
        else:
            await session.clear_session()

    assert container.documents == before


async def test_failed_atomic_batch_leaves_only_a_valid_sequence_gap() -> None:
    container = FakeContainerProxy()
    container.fail_batch_before_commit = FakeCosmosBatchOperationError(400)
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )

    with pytest.raises(FakeCosmosBatchOperationError):
        await session.add_items(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]
        )

    assert container.documents["__session_meta__"]["last_seq"] == 2
    assert not any(document.get("type") == "message" for document in container.documents.values())

    await session.add_items([{"role": "user", "content": "after failure"}])
    message = next(
        document for document in container.documents.values() if document.get("type") == "message"
    )
    assert message["seq"] == 2


async def test_write_reserved_before_clear_is_hidden_by_incarnation_fence() -> None:
    container = FakeContainerProxy()
    client = FakeCosmosClient(container)
    paused_session = PausedWriteSession(client)
    clearing_session = CosmosDBSession(
        "session-1",
        client=client,  # type: ignore[arg-type]
    )
    append = asyncio.create_task(paused_session.add_items([{"role": "user", "content": "stale"}]))
    await paused_session.reserved.wait()

    try:
        await clearing_session.clear_session()
    finally:
        paused_session.release_write.set()
    await append

    assert await clearing_session.get_items() == []
    assert any(document.get("type") == "message" for document in container.documents.values())


async def test_concurrent_pops_remove_distinct_items() -> None:
    container = ConcurrentPopContainer()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    await session.add_items(items)
    first_pop = asyncio.create_task(session.pop_item())
    second_pop = asyncio.create_task(session.pop_item())
    await container.pop_queries_ready.wait()

    container.release_pop_queries.set()
    popped = await asyncio.gather(first_pop, second_pop)

    assert sorted(
        cast("dict[str, str]", item)["content"] for item in popped if item is not None
    ) == ["first", "second"]
    assert await session.get_items() == []


async def test_cancelled_pop_waits_for_the_destructive_claim_to_settle() -> None:
    container = PausedDeleteContainer()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "settled"}])
    pop = asyncio.create_task(session.pop_item())
    await container.delete_started.wait()

    pop.cancel()
    container.release_delete.set()

    with pytest.raises(asyncio.CancelledError):
        await pop
    assert await session.get_items() == []


async def test_pop_reconciles_cosmos_timeout_after_delete() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    item: TResponseInputItem = {"role": "user", "content": "popped"}
    await session.add_items([item])
    container.fail_patch_after_apply = FakeCosmosClientTimeoutError("ambiguous claim")

    assert await session.pop_item() == item
    assert await session.get_items() == []


async def test_clear_reclaims_pop_tombstone_after_cleanup_failure() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    item: TResponseInputItem = {"role": "user", "content": "popped"}
    await session.add_items([item])
    container.fail_delete_before_commit = FakeCosmosClientTimeoutError("cleanup timed out")

    assert await session.pop_item() == item
    assert await session.get_items() == []
    assert any(document.get("type") == "pop_tombstone" for document in container.documents.values())

    await session.clear_session()

    assert not any(
        document.get("type") == "pop_tombstone" for document in container.documents.values()
    )


async def test_clear_cleanup_cannot_delete_a_new_incarnation_write() -> None:
    container = PausedCleanupContainer()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "old"}])
    clear = asyncio.create_task(session.clear_session())
    await container.cleanup_started.wait()
    new_item: TResponseInputItem = {"role": "assistant", "content": "new"}

    try:
        await session.add_items([new_item])
    finally:
        container.release_cleanup.set()
    await clear

    assert await session.get_items() == [new_item]


async def test_older_clear_cleanup_cannot_delete_a_newer_clear_write() -> None:
    container = PausedCleanupQueryContainer()
    client = FakeCosmosClient(container)
    older_session = CosmosDBSession(
        "session-1",
        client=client,  # type: ignore[arg-type]
    )
    newer_session = CosmosDBSession(
        "session-1",
        client=client,  # type: ignore[arg-type]
    )
    await older_session.add_items([{"role": "user", "content": "old"}])
    older_clear = asyncio.create_task(older_session.clear_session())
    await container.cleanup_started.wait()
    winner: TResponseInputItem = {"role": "assistant", "content": "winner"}

    try:
        await newer_session.clear_session()
        await newer_session.add_items([winner])
    finally:
        container.release_cleanup.set()
    await older_clear

    assert await newer_session.get_items() == [winner]


async def test_ambiguous_clear_patch_is_resolved_without_second_rotation() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "old"}])
    patch_attempts_before = container.patch_attempts
    container.fail_patch_after_apply = FakeCosmosHttpResponseError(503)

    await session.clear_session()

    assert container.patch_attempts == patch_attempts_before + 1
    assert container.documents["__session_meta__"]["last_seq"] == 0
    assert await session.get_items() == []


async def test_cancelled_clear_waits_for_the_incarnation_fence_to_settle() -> None:
    client = FakeCosmosClient()
    session = PausedClearSession(client)
    await session.add_items([{"role": "user", "content": "old"}])
    clear = asyncio.create_task(session.clear_session())
    await session.clear_started.wait()

    clear.cancel()
    session.release_clear.set()

    with pytest.raises(asyncio.CancelledError):
        await clear
    assert await session.get_items() == []


async def test_validate_container_checks_partition_index_ttl_and_queries() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        ttl_seconds=60,
    )

    result = await session.validate_container()

    assert result == ContainerValidationResult()
    assert result.is_valid
    assert container.read_calls == 1


async def test_validate_container_reports_sanitized_configuration_issues() -> None:
    container = FakeContainerProxy()
    container.properties = {
        "partitionKey": {"paths": ["/tenantId"]},
        "indexingPolicy": {
            "indexingMode": "consistent",
            "includedPaths": [{"path": "/*"}],
            "excludedPaths": [{"path": "/seq/?"}],
        },
    }
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
        ttl_seconds=60,
    )

    result = await session.validate_container()

    assert result.issue_codes == (
        "invalid_partition_key",
        "invalid_indexing_policy",
        "ttl_not_enabled",
    )
    with pytest.raises(CosmosSessionConfigurationError) as raised:
        result.raise_for_invalid()
    assert raised.value.code == "container_validation_failed"


async def test_validate_container_sanitizes_query_failure() -> None:
    secret = "SENTINEL-SERVICE-DETAIL"
    container = FakeContainerProxy()
    container.query_error = FakeCosmosHttpResponseError(400, headers={"detail": secret})
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )

    result = await session.validate_container()

    assert result.issue_codes == ("query_incompatible",)
    assert secret not in repr(result)


async def test_validate_container_sanitizes_cosmos_client_timeout() -> None:
    container = FakeContainerProxy()
    container.read_error = FakeCosmosClientTimeoutError("timed out")
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )

    result = await session.validate_container()

    assert result.issue_codes == ("container_unreachable",)


async def test_injected_client_must_disable_non_idempotent_write_retries() -> None:
    client = FakeCosmosClient(retry_write=1)

    with pytest.raises(ValueError, match="retry_write=False"):
        CosmosDBSession(
            "session-1",
            client=client,  # type: ignore[arg-type]
        )


async def test_injected_client_must_use_session_consistency() -> None:
    client = FakeCosmosClient(consistency_level="Eventual")

    with pytest.raises(ValueError, match="Session consistency"):
        CosmosDBSession(
            "session-1",
            client=client,  # type: ignore[arg-type]
        )


async def test_connection_string_factory_owns_session_consistent_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    factory = Mock(return_value=client)
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        factory,
    )

    session = await CosmosDBSession.from_connection_string(
        "session-1",
        "AccountEndpoint=https://example.invalid/;AccountKey=secret",
    )

    factory.assert_called_once_with(
        "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        credential=None,
        consistency_level="Session",
        retry_write=False,
    )
    assert client.entered
    assert client.database.read_calls == 1
    assert client.container.read_calls == 1
    await session.close()
    assert client.closed


async def test_token_credential_factory_fixes_session_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.extensions.memory import cosmosdb_session

    client = FakeCosmosClient()
    factory = Mock(return_value=client)
    monkeypatch.setattr(cosmosdb_session, "CosmosClient", factory)
    credential = object()

    session = await CosmosDBSession.from_token_credential(
        "session-1",
        "https://example.invalid/",
        credential,  # type: ignore[arg-type]
        validate_on_create=False,
    )

    factory.assert_called_once_with(
        "https://example.invalid/",
        credential,
        consistency_level="Session",
        retry_write=False,
    )
    await session.close()
    assert client.closed


async def test_factory_closes_client_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    client.container.properties["partitionKey"] = {"paths": ["/tenantId"]}
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        Mock(return_value=client),
    )

    with pytest.raises(CosmosSessionConfigurationError):
        await CosmosDBSession.from_connection_string(
            "session-1",
            "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        )

    assert client.closed


async def test_factory_cleanup_failure_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    client.container.properties["partitionKey"] = {"paths": ["/tenantId"]}
    close = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    client.close = close  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        Mock(return_value=client),
    )

    with pytest.raises(CosmosSessionConfigurationError):
        await CosmosDBSession.from_connection_string(
            "session-1",
            "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        )

    close.assert_awaited_once()


async def test_factory_cancellation_closes_created_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    validation_started = asyncio.Event()
    never_complete = asyncio.Event()

    async def pause_validation(**kwargs: Any) -> dict[str, str]:
        del kwargs
        validation_started.set()
        await never_complete.wait()
        return {"id": "agents"}

    client.database.read = pause_validation  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        Mock(return_value=client),
    )
    factory = asyncio.create_task(
        CosmosDBSession.from_connection_string(
            "session-1",
            "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        )
    )
    await validation_started.wait()

    factory.cancel()

    with pytest.raises(asyncio.CancelledError):
        await factory
    assert client.closed


async def test_failed_owned_close_is_terminal_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    close = AsyncMock(side_effect=[RuntimeError("close failed"), None])
    client.close = close  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        Mock(return_value=client),
    )
    session = await CosmosDBSession.from_connection_string(
        "session-1",
        "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        validate_on_create=False,
    )

    with pytest.raises(RuntimeError, match="close failed"):
        await session.close()
    with pytest.raises(RuntimeError, match="closed"):
        await session.get_items()

    await session.close()
    assert close.await_count == 2


async def test_concurrent_owned_close_shares_one_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def pause_close() -> None:
        close_started.set()
        await release_close.wait()

    close = AsyncMock(side_effect=pause_close)
    client.close = close  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        Mock(return_value=client),
    )
    session = await CosmosDBSession.from_connection_string(
        "session-1",
        "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        validate_on_create=False,
    )
    first_close = asyncio.create_task(session.close())
    await close_started.wait()
    second_close = asyncio.create_task(session.close())

    release_close.set()
    await asyncio.gather(first_close, second_close)

    assert close.await_count == 1


async def test_cancelled_owned_close_settles_one_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeCosmosClient()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def pause_close() -> None:
        close_started.set()
        await release_close.wait()
        client.closed = True

    close = AsyncMock(side_effect=pause_close)
    client.close = close  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agents.extensions.memory.cosmosdb_session.CosmosClient.from_connection_string",
        Mock(return_value=client),
    )
    session = await CosmosDBSession.from_connection_string(
        "session-1",
        "AccountEndpoint=https://example.invalid/;AccountKey=secret",
        validate_on_create=False,
    )
    closing = asyncio.create_task(session.close())
    await close_started.wait()

    closing.cancel()
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert client.closed
    with pytest.raises(RuntimeError, match="closed"):
        await session.get_items()

    await session.close()
    assert close.await_count == 1


async def test_close_preserves_injected_client_and_blocks_future_operations() -> None:
    client = FakeCosmosClient()
    session = CosmosDBSession(
        "session-1",
        client=client,  # type: ignore[arg-type]
    )
    assert await session.ping()

    await session.close()

    assert not client.closed
    with pytest.raises(RuntimeError, match="closed"):
        await session.get_items()


async def test_ping_returns_false_for_service_failure() -> None:
    client = FakeCosmosClient()
    client.database.read_error = FakeServiceRequestError("unreachable")
    session = CosmosDBSession(
        "session-1",
        client=client,  # type: ignore[arg-type]
    )

    assert not await session.ping()


async def test_ping_returns_false_for_cosmos_client_timeout() -> None:
    client = FakeCosmosClient()
    client.database.read_error = FakeCosmosClientTimeoutError("timed out")
    session = CosmosDBSession(
        "session-1",
        client=client,  # type: ignore[arg-type]
    )

    assert not await session.ping()


async def test_delete_session_physically_removes_quiescent_partition() -> None:
    container = FakeContainerProxy()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "hello"}])

    await session.delete_session()

    assert container.documents == {}
    delete_operations = [
        operations
        for operations in container.batch_calls
        if operations and all(operation == "delete" for operation, _ in operations)
    ]
    assert delete_operations[-1][-1][1][0] == "__session_meta__"


async def test_cancelled_delete_waits_for_partition_cleanup_to_settle() -> None:
    container = PausedCleanupContainer()
    session = CosmosDBSession(
        "session-1",
        client=FakeCosmosClient(container),  # type: ignore[arg-type]
    )
    await session.add_items([{"role": "user", "content": "delete me"}])
    delete = asyncio.create_task(session.delete_session())
    await container.cleanup_started.wait()

    delete.cancel()
    container.release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await delete
    assert container.documents == {}
