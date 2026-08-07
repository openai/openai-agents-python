"""Azure Cosmos DB session backend for the OpenAI Agents SDK."""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ._optional_imports import raise_optional_dependency_error

try:
    from azure.core import MatchConditions
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.core.exceptions import ServiceRequestError, ServiceResponseError
    from azure.cosmos.aio import ContainerProxy, CosmosClient, DatabaseProxy
    from azure.cosmos.exceptions import (
        CosmosBatchOperationError,
        CosmosClientTimeoutError,
        CosmosHttpResponseError,
    )
except ImportError as error:
    raise_optional_dependency_error(
        "CosmosDBSession",
        dependency_name="azure-cosmos",
        extra_name="cosmosdb",
        cause=error,
    )

from ...memory.session import SessionABC
from ...memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)
from ...memory.sqlite_session import _await_mutation

if TYPE_CHECKING:
    from ...items import TResponseInputItem

SCHEMA_VERSION = 2
META_ID = "__session_meta__"
POP_TOMBSTONE_TYPE = "pop_tombstone"
MAX_BATCH_ITEMS = 100
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_REQUIRED_INDEX_PATHS = frozenset({"/type/?", "/incarnation/?", "/seq/?"})
_MESSAGE_DOCUMENT_FIELDS = (
    "id",
    "sessionId",
    "type",
    "schema_version",
    "incarnation",
    "seq",
    "message_data",
    "created_at",
    "ttl",
)


@dataclass(frozen=True)
class _RetryDelayPolicy:
    base_delay_seconds: float
    max_delay_seconds: float
    random_value: Callable[[], float] = field(
        default=random.random,
        repr=False,
        compare=False,
    )
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc),
        repr=False,
        compare=False,
    )

    def delay_seconds(self, error: BaseException, retry_number: int) -> float:
        server_hint = _server_retry_hint_seconds(error, now=self.now)
        try:
            exponential_cap = math.ldexp(self.base_delay_seconds, retry_number)
        except OverflowError:
            exponential_cap = self.max_delay_seconds
        client_cap = min(exponential_cap, self.max_delay_seconds)
        remaining_below_max = max(0.0, self.max_delay_seconds - server_hint)
        jitter_cap = min(client_cap, remaining_below_max)
        random_fraction = min(1.0, max(0.0, self.random_value()))
        return server_hint + (random_fraction * jitter_cap)


class CosmosSessionError(RuntimeError):
    """Base exception for Cosmos DB session failures."""


class CosmosSessionConflictError(CosmosSessionError):
    """Raised when persisted session state conflicts with the expected schema."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        mismatched_fields: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.mismatched_fields = mismatched_fields


class CosmosSessionCorruptDocumentError(CosmosSessionError):
    """Raised when a stored session document cannot define a safe history boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        invalid_fields: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.invalid_fields = invalid_fields


class CosmosSessionConfigurationError(CosmosSessionError):
    """Raised when Cosmos DB session or container configuration is invalid."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        invalid_fields: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.invalid_fields = invalid_fields


@dataclass(frozen=True)
class ContainerValidationResult:
    """Sanitized readiness findings for one Cosmos DB container."""

    issue_codes: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Return whether the container satisfies the session contract."""
        return not self.issue_codes

    def raise_for_invalid(self) -> None:
        """Raise a sanitized configuration error when validation found issues."""
        if self.is_valid:
            return
        raise CosmosSessionConfigurationError(
            "The Cosmos DB container does not satisfy the session contract.",
            code="container_validation_failed",
            invalid_fields=self.issue_codes,
        )


class CosmosDBSession(SessionABC):
    """Store one Agents SDK session in an Azure Cosmos DB partition.

    Injected clients remain caller-owned and must use Session consistency with
    ``retry_write=False``. The factory methods create clients with those settings
    and transfer client ownership to this instance.
    """

    def __init__(
        self,
        session_id: str,
        *,
        client: CosmosClient,
        database: str = "agents",
        container: str = "agent_sessions",
        session_settings: SessionSettings | dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        max_write_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        max_retry_delay_seconds: float = 30.0,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_write_attempts < 1:
            raise ValueError("max_write_attempts must be at least 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if max_retry_delay_seconds < 0:
            raise ValueError("max_retry_delay_seconds must not be negative")
        if _client_retries_non_idempotent_writes(client):
            raise ValueError("client must be configured with retry_write=False")
        if not _client_uses_session_consistency(client):
            raise ValueError("client must be configured with Session consistency")

        self.session_id = session_id
        self.session_settings = (
            coerce_session_settings(session_settings) if session_settings is not None else None
        )
        self._client = client
        self._database: DatabaseProxy = client.get_database_client(database)
        self._container: ContainerProxy = self._database.get_container_client(container)
        self._ttl_seconds = ttl_seconds
        self._max_write_attempts = max_write_attempts
        self._retry_delay_policy = _RetryDelayPolicy(
            base_delay_seconds=retry_backoff_seconds,
            max_delay_seconds=max_retry_delay_seconds,
        )
        self._owns_client = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def from_connection_string(
        cls,
        session_id: str,
        connection_string: str,
        *,
        credential: str | dict[str, str] | None = None,
        validate_on_create: bool = True,
        database: str = "agents",
        container: str = "agent_sessions",
        session_settings: SessionSettings | dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        max_write_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        max_retry_delay_seconds: float = 30.0,
    ) -> CosmosDBSession:
        """Create a session that owns a client built from a connection string."""
        client = CosmosClient.from_connection_string(
            connection_string,
            credential=credential,
            consistency_level="Session",
            retry_write=False,
        )
        try:
            await client.__aenter__()
            session = cls(
                session_id,
                client=client,
                database=database,
                container=container,
                session_settings=session_settings,
                ttl_seconds=ttl_seconds,
                max_write_attempts=max_write_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                max_retry_delay_seconds=max_retry_delay_seconds,
            )
            if validate_on_create:
                (await session.validate_container()).raise_for_invalid()
        except BaseException:
            await _close_client_after_factory_failure(client)
            raise
        session._owns_client = True
        return session

    @classmethod
    async def from_token_credential(
        cls,
        session_id: str,
        endpoint: str,
        credential: AsyncTokenCredential,
        *,
        validate_on_create: bool = True,
        database: str = "agents",
        container: str = "agent_sessions",
        session_settings: SessionSettings | dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        max_write_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        max_retry_delay_seconds: float = 30.0,
    ) -> CosmosDBSession:
        """Create a session that owns a client authenticated by a token credential."""
        return await cls.from_credential(
            session_id,
            endpoint,
            credential,
            validate_on_create=validate_on_create,
            database=database,
            container=container,
            session_settings=session_settings,
            ttl_seconds=ttl_seconds,
            max_write_attempts=max_write_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            max_retry_delay_seconds=max_retry_delay_seconds,
        )

    @classmethod
    async def from_credential(
        cls,
        session_id: str,
        endpoint: str,
        credential: str | dict[str, str] | AsyncTokenCredential,
        *,
        validate_on_create: bool = True,
        database: str = "agents",
        container: str = "agent_sessions",
        session_settings: SessionSettings | dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        max_write_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        max_retry_delay_seconds: float = 30.0,
    ) -> CosmosDBSession:
        """Create a session that owns a client built from an endpoint and credential."""
        client = CosmosClient(
            endpoint,
            credential,
            consistency_level="Session",
            retry_write=False,
        )
        try:
            await client.__aenter__()
            session = cls(
                session_id,
                client=client,
                database=database,
                container=container,
                session_settings=session_settings,
                ttl_seconds=ttl_seconds,
                max_write_attempts=max_write_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                max_retry_delay_seconds=max_retry_delay_seconds,
            )
            if validate_on_create:
                (await session.validate_container()).raise_for_invalid()
        except BaseException:
            await _close_client_after_factory_failure(client)
            raise
        session._owns_client = True
        return session

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Return the latest session items in chronological order."""
        self._check_not_closed()
        effective_limit = resolve_session_limit(limit, self.session_settings)
        if effective_limit is not None and effective_limit <= 0:
            return []

        incarnation = await self._read_current_incarnation()
        if incarnation is None:
            return []

        parameters: list[dict[str, Any]] = [{"name": "@incarnation", "value": incarnation}]
        if effective_limit is None:
            query = (
                "SELECT * FROM c WHERE c.type = 'message' "
                "AND c.incarnation = @incarnation ORDER BY c.seq ASC"
            )
        else:
            query = (
                "SELECT * FROM c WHERE c.type = 'message' "
                "AND c.incarnation = @incarnation ORDER BY c.seq DESC"
            )

        items: list[TResponseInputItem] = []
        documents = self._container.query_items(
            query=query,
            parameters=parameters,
            partition_key=self.session_id,
        )
        async for document in documents:
            try:
                items.append(_deserialize_document(document))
            except CosmosSessionCorruptDocumentError:
                continue
            if effective_limit is not None and len(items) == effective_limit:
                break

        if effective_limit is not None:
            items.reverse()
        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Append one atomic logical batch to the session."""
        self._check_not_closed()
        if not items:
            return
        if len(items) > MAX_BATCH_ITEMS:
            raise ValueError(f"add_items supports at most {MAX_BATCH_ITEMS} items per atomic batch")

        serialized_items = [_serialize_item(item) for item in items]
        await _await_mutation(self._add_items(serialized_items))

    async def _add_items(self, serialized_items: list[str]) -> None:
        incarnation, first_seq = await self._reserve_sequence_block(len(serialized_items))
        documents = [
            _build_message_document(
                seq=first_seq + offset,
                message_data=message_data,
                session_id=self.session_id,
                incarnation=incarnation,
                ttl_seconds=self._ttl_seconds,
            )
            for offset, message_data in enumerate(serialized_items)
        ]
        await self._write_reserved_batch_with_retry(documents)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the latest session item."""
        self._check_not_closed()
        return await _await_mutation(self._pop_item())

    async def _pop_item(self) -> TResponseInputItem | None:
        query = (
            "SELECT * FROM c WHERE c.type = 'message' "
            "AND c.incarnation = @incarnation ORDER BY c.seq DESC"
        )

        while True:
            incarnation = await self._read_current_incarnation()
            if incarnation is None:
                return None
            documents = self._container.query_items(
                query=query,
                parameters=[{"name": "@incarnation", "value": incarnation}],
                partition_key=self.session_id,
            )
            retry = False
            async for document in documents:
                item_id = document.get("id") if isinstance(document, Mapping) else None
                etag = document.get("_etag") if isinstance(document, Mapping) else None
                if not isinstance(item_id, str) or not isinstance(etag, str):
                    continue
                try:
                    item = _deserialize_document(document)
                except CosmosSessionCorruptDocumentError:
                    continue
                claim_etag = await self._claim_item_for_pop(
                    item_id=item_id,
                    etag=etag,
                    incarnation=incarnation,
                )
                if claim_etag is None:
                    retry = True
                    break
                await self._cleanup_pop_tombstone(item_id, claim_etag)
                return item
            if not retry:
                return None

    async def _claim_item_for_pop(
        self,
        *,
        item_id: str,
        etag: str,
        incarnation: str,
    ) -> str | None:
        claim_id = uuid4().hex
        patch_operations = [
            {"op": "set", "path": "/type", "value": POP_TOMBSTONE_TYPE},
            {"op": "set", "path": "/pop_claim", "value": claim_id},
        ]
        ambiguous_error: BaseException | None = None

        for attempt in range(self._max_write_attempts):
            try:
                claimed = await self._container.patch_item(
                    item=item_id,
                    partition_key=self.session_id,
                    patch_operations=patch_operations,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                    no_response=False,
                    retry_write=0,
                )
            except CosmosHttpResponseError as error:
                if error.status_code == 404:
                    return None
                if error.status_code == 412:
                    if ambiguous_error is None:
                        return None
                elif not _is_retryable_write_error(error):
                    raise
                else:
                    ambiguous_error = error
            except (CosmosClientTimeoutError, ServiceRequestError, ServiceResponseError) as error:
                ambiguous_error = error
            else:
                return _validate_pop_claim(
                    claimed,
                    item_id=item_id,
                    session_id=self.session_id,
                    incarnation=incarnation,
                    claim_id=claim_id,
                )

            try:
                claim_state, claim_etag = await self._read_pop_claim_state(
                    item_id=item_id,
                    expected_etag=etag,
                    incarnation=incarnation,
                    claim_id=claim_id,
                )
            except CosmosHttpResponseError as read_error:
                if not _is_retryable_write_error(read_error):
                    raise
                if attempt + 1 == self._max_write_attempts:
                    assert ambiguous_error is not None
                    raise ambiguous_error from None
            except (CosmosClientTimeoutError, ServiceRequestError, ServiceResponseError):
                if attempt + 1 == self._max_write_attempts:
                    assert ambiguous_error is not None
                    raise ambiguous_error from None
            else:
                if claim_state == "claimed":
                    assert claim_etag is not None
                    return claim_etag
                if claim_state != "unclaimed":
                    return None

            if attempt + 1 == self._max_write_attempts:
                assert ambiguous_error is not None
                raise ambiguous_error from None
            delay = self._retry_delay_policy.delay_seconds(ambiguous_error, attempt)
            if delay > 0:
                await asyncio.sleep(delay)

        raise AssertionError("pop claim retry loop exited unexpectedly")

    async def _read_pop_claim_state(
        self,
        *,
        item_id: str,
        expected_etag: str,
        incarnation: str,
        claim_id: str,
    ) -> tuple[str, str | None]:
        try:
            document = await self._container.read_item(
                item=item_id,
                partition_key=self.session_id,
            )
        except CosmosHttpResponseError as error:
            if error.status_code == 404:
                return "missing", None
            raise

        document_etag = document.get("_etag")
        if document.get("type") == POP_TOMBSTONE_TYPE and document.get("pop_claim") == claim_id:
            return "claimed", _validate_pop_claim(
                document,
                item_id=item_id,
                session_id=self.session_id,
                incarnation=incarnation,
                claim_id=claim_id,
            )
        if document.get("type") == "message" and document_etag == expected_etag:
            return "unclaimed", document_etag
        return "other", document_etag if isinstance(document_etag, str) else None

    async def _cleanup_pop_tombstone(self, item_id: str, etag: str) -> None:
        try:
            await self._container.delete_item(
                item=item_id,
                partition_key=self.session_id,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                retry_write=0,
            )
        except (
            CosmosClientTimeoutError,
            CosmosHttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
        ):
            return

    async def clear_session(self) -> None:
        """Clear all items in this session."""
        self._check_not_closed()
        await _await_mutation(self._clear_session())

    async def _clear_session(self) -> None:
        _, previous_incarnation = await self._rotate_session_incarnation()
        if previous_incarnation is not None:
            await self._cleanup_incarnation(previous_incarnation)

    async def delete_session(self) -> None:
        """Physically delete a quiescent session, including its metadata fence."""
        self._check_not_closed()
        await _await_mutation(self._delete_session())

    async def _delete_session(self) -> None:
        results = self._container.query_items(
            query="SELECT VALUE c.id FROM c",
            partition_key=self.session_id,
        )
        item_ids: list[str] = []
        async for item_id in results:
            if isinstance(item_id, str):
                item_ids.append(item_id)
        item_ids.sort(key=lambda item_id: item_id == META_ID)
        for offset in range(0, len(item_ids), MAX_BATCH_ITEMS):
            operations = [
                ("delete", (item_id,)) for item_id in item_ids[offset : offset + MAX_BATCH_ITEMS]
            ]
            await self._container.execute_item_batch(
                batch_operations=operations,
                partition_key=self.session_id,
                retry_write=0,
            )

    async def close(self) -> None:
        """Close an owned client and mark this session closed."""
        self._closed = True
        if not self._owns_client:
            return

        close_task = self._close_task
        if close_task is not None and close_task.done():
            if not close_task.cancelled() and close_task.exception() is None:
                return
            close_task = None
        if close_task is None:
            close_task = asyncio.create_task(self._client.close())
            self._close_task = close_task
        await _await_mutation(close_task)

    async def ping(self) -> bool:
        """Return whether the configured database can be read."""
        self._check_not_closed()
        try:
            await self._database.read()
        except (
            CosmosClientTimeoutError,
            CosmosHttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
        ):
            return False
        return True

    async def validate_container(self) -> ContainerValidationResult:
        """Validate the configured container without writing application data."""
        self._check_not_closed()
        return await _validate_container(
            database=self._database,
            container=self._container,
            ttl_seconds=self._ttl_seconds,
        )

    async def _reserve_sequence_block(self, count: int) -> tuple[str, int]:
        while True:
            now = _utc_now()
            try:
                metadata = await self._container.read_item(
                    item=META_ID,
                    partition_key=self.session_id,
                )
            except CosmosHttpResponseError as error:
                if error.status_code != 404:
                    raise
                await self._create_metadata_if_missing(now)
                continue

            incarnation, last_seq, etag = _validate_metadata(metadata, self.session_id)
            patch_operations: list[dict[str, Any]] = [
                {"op": "incr", "path": "/last_seq", "value": count},
                {"op": "set", "path": "/updated_at", "value": now},
            ]
            if self._ttl_seconds is not None:
                patch_operations.append({"op": "set", "path": "/ttl", "value": self._ttl_seconds})
            try:
                metadata = await self._container.patch_item(
                    item=META_ID,
                    partition_key=self.session_id,
                    patch_operations=patch_operations,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                    no_response=False,
                    retry_write=0,
                )
            except CosmosHttpResponseError as error:
                if error.status_code == 404:
                    await self._create_metadata_if_missing(now)
                    continue
                if error.status_code == 412:
                    continue
                raise

            updated_incarnation, updated_last_seq, _ = _validate_metadata(
                metadata,
                self.session_id,
            )
            mismatched_fields: list[str] = []
            if updated_incarnation != incarnation:
                mismatched_fields.append("incarnation")
            if updated_last_seq != last_seq + count:
                mismatched_fields.append("last_seq")
            if mismatched_fields:
                raise CosmosSessionConflictError(
                    "The session metadata returned an invalid sequence reservation.",
                    code="invalid_session_metadata",
                    mismatched_fields=tuple(mismatched_fields),
                )
            return incarnation, last_seq

    async def _write_reserved_batch_with_retry(
        self,
        documents: Sequence[dict[str, Any]],
    ) -> None:
        operations = [("create", (document,)) for document in documents]
        for attempt in range(self._max_write_attempts):
            try:
                await self._container.execute_item_batch(
                    batch_operations=operations,
                    partition_key=self.session_id,
                    retry_write=0,
                )
                return
            except (
                CosmosBatchOperationError,
                CosmosClientTimeoutError,
                CosmosHttpResponseError,
                ServiceRequestError,
                ServiceResponseError,
            ) as error:
                if getattr(error, "status_code", None) == 409:
                    await self._validate_replayed_batch(documents)
                    return
                if not _is_retryable_write_error(error):
                    raise
                if attempt + 1 == self._max_write_attempts:
                    try:
                        committed = await self._reserved_batch_matches(documents)
                    except (
                        CosmosClientTimeoutError,
                        CosmosHttpResponseError,
                        ServiceRequestError,
                        ServiceResponseError,
                    ):
                        raise error from None
                    if committed:
                        return
                    raise
                delay = self._retry_delay_policy.delay_seconds(error, attempt)
                if delay > 0:
                    await asyncio.sleep(delay)

        raise AssertionError("write retry loop exited unexpectedly")

    async def _validate_replayed_batch(
        self,
        expected_documents: Sequence[dict[str, Any]],
    ) -> None:
        if await self._reserved_batch_matches(expected_documents):
            return
        raise CosmosSessionConflictError(
            "A reserved message document is missing after a batch conflict.",
            code="reserved_message_mismatch",
            mismatched_fields=("id",),
        )

    async def _reserved_batch_matches(
        self,
        expected_documents: Sequence[dict[str, Any]],
    ) -> bool:
        matched_count = 0
        missing_count = 0
        for expected_document in expected_documents:
            try:
                existing = await self._container.read_item(
                    item=expected_document["id"],
                    partition_key=self.session_id,
                )
            except CosmosHttpResponseError as error:
                if error.status_code != 404:
                    raise
                missing_count += 1
                continue

            mismatched_fields = _document_mismatched_fields(existing, expected_document)
            if mismatched_fields:
                raise CosmosSessionConflictError(
                    "A document with a reserved ID does not match the replayed item.",
                    code="reserved_message_mismatch",
                    mismatched_fields=mismatched_fields,
                ) from None
            matched_count += 1

        if matched_count and missing_count:
            raise CosmosSessionConflictError(
                "Only part of a reserved message batch exists.",
                code="reserved_message_mismatch",
                mismatched_fields=("id",),
            )
        return missing_count == 0

    async def _rotate_session_incarnation(self) -> tuple[str, str | None]:
        incarnation = uuid4().hex
        while True:
            now = _utc_now()
            try:
                metadata = await self._container.read_item(
                    item=META_ID,
                    partition_key=self.session_id,
                )
            except CosmosHttpResponseError as error:
                if error.status_code != 404:
                    raise
                if await self._create_metadata_if_missing(now, incarnation=incarnation):
                    return incarnation, None
                continue

            previous_incarnation, _, etag = _validate_metadata(metadata, self.session_id)
            patch_operations: list[dict[str, Any]] = [
                {"op": "set", "path": "/incarnation", "value": incarnation},
                {"op": "set", "path": "/last_seq", "value": 0},
                {"op": "set", "path": "/updated_at", "value": now},
            ]
            if self._ttl_seconds is not None:
                patch_operations.append({"op": "set", "path": "/ttl", "value": self._ttl_seconds})
            try:
                updated_metadata = await self._container.patch_item(
                    item=META_ID,
                    partition_key=self.session_id,
                    patch_operations=patch_operations,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                    no_response=False,
                    retry_write=0,
                )
            except (
                CosmosClientTimeoutError,
                CosmosHttpResponseError,
                ServiceRequestError,
                ServiceResponseError,
            ) as error:
                if getattr(error, "status_code", None) in {404, 412}:
                    continue
                if not _is_retryable_write_error(error):
                    raise
                try:
                    metadata = await self._container.read_item(
                        item=META_ID,
                        partition_key=self.session_id,
                    )
                except (
                    CosmosClientTimeoutError,
                    CosmosHttpResponseError,
                    ServiceRequestError,
                    ServiceResponseError,
                ):
                    raise error from None
                stored_incarnation, _, _ = _validate_metadata(metadata, self.session_id)
                if stored_incarnation == incarnation:
                    return incarnation, previous_incarnation
                raise error from None

            stored_incarnation, stored_last_seq, _ = _validate_metadata(
                updated_metadata,
                self.session_id,
            )
            mismatched_fields: list[str] = []
            if stored_incarnation != incarnation:
                mismatched_fields.append("incarnation")
            if stored_last_seq != 0:
                mismatched_fields.append("last_seq")
            if mismatched_fields:
                raise CosmosSessionConflictError(
                    "The session metadata returned an invalid clear boundary.",
                    code="invalid_session_metadata",
                    mismatched_fields=tuple(mismatched_fields),
                )
            return incarnation, previous_incarnation

    async def _cleanup_incarnation(self, incarnation: str) -> None:
        results = self._container.query_items(
            query=(
                "SELECT VALUE c.id FROM c WHERE "
                "(c.type = 'message' OR c.type = 'pop_tombstone') "
                "AND c.incarnation = @incarnation"
            ),
            parameters=[{"name": "@incarnation", "value": incarnation}],
            partition_key=self.session_id,
        )
        item_ids: list[str] = []
        try:
            async for item_id in results:
                if isinstance(item_id, str):
                    item_ids.append(item_id)

            for offset in range(0, len(item_ids), MAX_BATCH_ITEMS):
                operations = [
                    ("delete", (item_id,))
                    for item_id in item_ids[offset : offset + MAX_BATCH_ITEMS]
                ]
                await self._container.execute_item_batch(
                    batch_operations=operations,
                    partition_key=self.session_id,
                    retry_write=0,
                )
        except (
            CosmosBatchOperationError,
            CosmosClientTimeoutError,
            CosmosHttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
        ):
            return

    async def _create_metadata_if_missing(
        self,
        now: str,
        *,
        incarnation: str | None = None,
    ) -> bool:
        metadata: dict[str, Any] = {
            "id": META_ID,
            "sessionId": self.session_id,
            "type": "meta",
            "schema_version": SCHEMA_VERSION,
            "incarnation": incarnation or uuid4().hex,
            "last_seq": 0,
            "created_at": now,
            "updated_at": now,
        }
        if self._ttl_seconds is not None:
            metadata["ttl"] = self._ttl_seconds

        try:
            await self._container.create_item(body=metadata, retry_write=0)
            return True
        except CosmosHttpResponseError as error:
            if error.status_code != 409:
                raise
            existing = await self._container.read_item(
                item=META_ID,
                partition_key=self.session_id,
            )
            _validate_metadata(existing, self.session_id)
            return False

    async def _read_current_incarnation(self) -> str | None:
        try:
            metadata = await self._container.read_item(
                item=META_ID,
                partition_key=self.session_id,
            )
        except CosmosHttpResponseError as error:
            if error.status_code == 404:
                return None
            raise
        incarnation, _, _ = _validate_metadata(metadata, self.session_id)
        return incarnation

    def _check_not_closed(self) -> None:
        if self._closed:
            raise RuntimeError("CosmosDBSession is closed")


def _validate_metadata(metadata: Mapping[str, Any], session_id: str) -> tuple[str, int, str]:
    invalid_fields: list[str] = []
    incarnation = metadata.get("incarnation")
    last_seq = metadata.get("last_seq")
    etag = metadata.get("_etag")
    if metadata.get("sessionId") != session_id:
        invalid_fields.append("sessionId")
    if metadata.get("type") != "meta":
        invalid_fields.append("type")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        invalid_fields.append("schema_version")
    if not isinstance(incarnation, str) or not incarnation:
        invalid_fields.append("incarnation")
    if isinstance(last_seq, bool) or not isinstance(last_seq, int) or last_seq < 0:
        invalid_fields.append("last_seq")
    if not isinstance(etag, str) or not etag:
        invalid_fields.append("_etag")
    if invalid_fields:
        raise CosmosSessionConflictError(
            "The session metadata is not valid schema-v2 metadata.",
            code="invalid_session_metadata",
            mismatched_fields=tuple(invalid_fields),
        )
    assert isinstance(incarnation, str)
    assert isinstance(last_seq, int)
    assert isinstance(etag, str)
    return incarnation, last_seq, etag


def _serialize_item(item: TResponseInputItem) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


def _deserialize_document(document: Mapping[str, Any]) -> TResponseInputItem:
    invalid_fields: list[str] = []
    document_id = document.get("id")
    session_id = document.get("sessionId")
    incarnation = document.get("incarnation")
    seq = document.get("seq")
    message_data = document.get("message_data")
    if not isinstance(document_id, str):
        invalid_fields.append("id")
    if not isinstance(session_id, str) or not session_id:
        invalid_fields.append("sessionId")
    if document.get("type") != "message":
        invalid_fields.append("type")
    if document.get("schema_version") != SCHEMA_VERSION:
        invalid_fields.append("schema_version")
    if not isinstance(incarnation, str) or not incarnation:
        invalid_fields.append("incarnation")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        invalid_fields.append("seq")
    if not isinstance(message_data, str):
        invalid_fields.append("message_data")
    if (
        isinstance(document_id, str)
        and isinstance(incarnation, str)
        and isinstance(seq, int)
        and not isinstance(seq, bool)
        and document_id != f"{incarnation}:{seq:012d}"
    ):
        invalid_fields.append("id")
    if invalid_fields:
        raise CosmosSessionCorruptDocumentError(
            "A stored session message failed schema validation.",
            code="invalid_message_document",
            invalid_fields=tuple(dict.fromkeys(invalid_fields)),
        )

    assert isinstance(message_data, str)
    try:
        item = json.loads(message_data)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise CosmosSessionCorruptDocumentError(
            "A stored session message payload could not be decoded.",
            code="invalid_message_payload",
            invalid_fields=("message_data",),
        ) from None
    if not isinstance(item, Mapping):
        raise CosmosSessionCorruptDocumentError(
            "A stored session message payload has an invalid shape.",
            code="invalid_message_shape",
            invalid_fields=("message_data",),
        )
    return cast("TResponseInputItem", item)


def _build_message_document(
    *,
    seq: int,
    message_data: str,
    session_id: str,
    incarnation: str,
    ttl_seconds: int | None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": f"{incarnation}:{seq:012d}",
        "sessionId": session_id,
        "type": "message",
        "schema_version": SCHEMA_VERSION,
        "incarnation": incarnation,
        "seq": seq,
        "message_data": message_data,
        "created_at": _utc_now(),
    }
    if ttl_seconds is not None:
        document["ttl"] = ttl_seconds
    return document


def _document_mismatched_fields(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(
        field_name
        for field_name in _MESSAGE_DOCUMENT_FIELDS
        if (field_name in existing) != (field_name in expected)
        or existing.get(field_name) != expected.get(field_name)
    )


def _validate_pop_claim(
    document: Mapping[str, Any],
    *,
    item_id: str,
    session_id: str,
    incarnation: str,
    claim_id: str,
) -> str:
    expected = {
        "id": item_id,
        "sessionId": session_id,
        "type": POP_TOMBSTONE_TYPE,
        "schema_version": SCHEMA_VERSION,
        "incarnation": incarnation,
        "pop_claim": claim_id,
    }
    mismatched_fields = [
        field_name
        for field_name, expected_value in expected.items()
        if document.get(field_name) != expected_value
    ]
    etag = document.get("_etag")
    if not isinstance(etag, str) or not etag:
        mismatched_fields.append("_etag")
    if mismatched_fields:
        raise CosmosSessionConflictError(
            "The session message returned an invalid pop claim.",
            code="invalid_pop_claim",
            mismatched_fields=tuple(mismatched_fields),
        )
    assert isinstance(etag, str)
    return etag


def _client_retries_non_idempotent_writes(client: CosmosClient) -> bool:
    client_connection = getattr(client, "client_connection", None)
    connection_policy = getattr(client_connection, "connection_policy", None)
    retry_write = getattr(connection_policy, "RetryNonIdempotentWrites", False)
    return isinstance(retry_write, int) and retry_write > 0


def _client_uses_session_consistency(client: CosmosClient) -> bool:
    client_connection = getattr(client, "client_connection", None)
    default_headers = getattr(client_connection, "default_headers", None)
    if not isinstance(default_headers, Mapping):
        return False
    consistency_level = _get_header(default_headers, "x-ms-consistency-level")
    return consistency_level is not None and consistency_level.lower() == "session"


def _is_retryable_write_error(error: BaseException) -> bool:
    if isinstance(error, CosmosClientTimeoutError | ServiceRequestError | ServiceResponseError):
        return True
    return getattr(error, "status_code", None) in _RETRYABLE_STATUS_CODES


def _server_retry_hint_seconds(
    error: BaseException,
    *,
    now: Callable[[], datetime],
) -> float:
    headers = getattr(error, "headers", None)
    if not isinstance(headers, Mapping):
        return 0.0

    retry_after_ms = _get_header(headers, "x-ms-retry-after-ms")
    if retry_after_ms is not None:
        try:
            milliseconds = float(retry_after_ms)
        except (TypeError, ValueError):
            pass
        else:
            if milliseconds >= 0:
                return milliseconds / 1000.0

    retry_after = _get_header(headers, "retry-after")
    if retry_after is None:
        return 0.0
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - now()).total_seconds())
    return max(0.0, seconds)


def _get_header(headers: Mapping[object, object], expected_name: str) -> str | None:
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == expected_name and isinstance(value, str):
            return value
    return None


async def _validate_container(
    *,
    database: DatabaseProxy,
    container: ContainerProxy,
    ttl_seconds: int | None,
) -> ContainerValidationResult:
    try:
        await database.read()
    except (
        CosmosClientTimeoutError,
        CosmosHttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    ):
        return ContainerValidationResult(("database_unreachable",))

    try:
        properties = await container.read()
    except (
        CosmosClientTimeoutError,
        CosmosHttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    ):
        return ContainerValidationResult(("container_unreachable",))

    issues: list[str] = []
    partition_key = properties.get("partitionKey")
    partition_paths = partition_key.get("paths") if isinstance(partition_key, Mapping) else None
    if partition_paths != ["/sessionId"]:
        issues.append("invalid_partition_key")
    if not _has_required_index_paths(properties.get("indexingPolicy")):
        issues.append("invalid_indexing_policy")
    if ttl_seconds is not None and properties.get("defaultTtl") is None:
        issues.append("ttl_not_enabled")

    if "invalid_partition_key" not in issues and "invalid_indexing_policy" not in issues:
        query_issue = await _validate_query_shapes(container)
        if query_issue is not None:
            issues.append(query_issue)
    return ContainerValidationResult(tuple(issues))


async def _validate_query_shapes(container: ContainerProxy) -> str | None:
    validation_partition = f"__openai_cosmosdb_validation__:{uuid4().hex}"
    queries = (
        "SELECT * FROM c WHERE c.type = 'message' "
        "AND c.incarnation = @incarnation ORDER BY c.seq ASC",
        "SELECT * FROM c WHERE c.type = 'message' "
        "AND c.incarnation = @incarnation ORDER BY c.seq DESC",
    )
    try:
        for query in queries:
            results = container.query_items(
                query=query,
                parameters=[{"name": "@incarnation", "value": "validation"}],
                partition_key=validation_partition,
            )
            async for _ in results:
                break
    except (
        CosmosClientTimeoutError,
        CosmosHttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    ):
        return "query_incompatible"
    return None


def _has_required_index_paths(indexing_policy: object) -> bool:
    if not isinstance(indexing_policy, Mapping):
        return False
    indexing_mode = indexing_policy.get("indexingMode")
    if isinstance(indexing_mode, str) and indexing_mode.lower() == "none":
        return False

    included = _extract_paths(indexing_policy.get("includedPaths"))
    excluded = _extract_paths(indexing_policy.get("excludedPaths"))
    for required_path in _REQUIRED_INDEX_PATHS:
        if required_path in included:
            continue
        if "/*" in included and required_path not in excluded and "/*" not in excluded:
            continue
        return False
    return True


def _extract_paths(configured_paths: object) -> set[str]:
    if not isinstance(configured_paths, list):
        return set()
    paths: set[str] = set()
    for configured_path in configured_paths:
        if not isinstance(configured_path, Mapping):
            continue
        path = configured_path.get("path")
        if isinstance(path, str):
            paths.add(path)
    return paths


async def _close_client_after_factory_failure(client: CosmosClient) -> None:
    try:
        await _await_mutation(client.close())
    except BaseException:
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ContainerValidationResult",
    "CosmosDBSession",
    "CosmosSessionConfigurationError",
    "CosmosSessionConflictError",
    "CosmosSessionCorruptDocumentError",
    "CosmosSessionError",
]
