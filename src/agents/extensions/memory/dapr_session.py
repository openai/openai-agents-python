"""Dapr State Store-powered Session backend.

Usage::

    from agents.extensions.memory import DaprSession

    # Create from Dapr sidecar address
    session = DaprSession.from_address(
        session_id="user-123",
        state_store_name="statestore",
        dapr_address="localhost:50001",
    )

    # Or pass an existing Dapr client that your application already manages
    session = DaprSession(
        session_id="user-123",
        state_store_name="statestore",
        dapr_client=my_dapr_client,
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Final, Literal

from ._optional_imports import raise_optional_dependency_error

try:
    from dapr.aio.clients import DaprClient
    from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions
except ImportError as e:
    raise_optional_dependency_error(
        "DaprSession",
        dependency_name="dapr",
        extra_name="dapr",
        cause=e,
    )

from ...items import TResponseInputItem
from ...logger import (
    log_model_and_tool_action_error,
    log_model_and_tool_action_warning,
    logger,
)
from ...memory.session import SessionABC
from ...memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)

# Type alias for consistency levels
ConsistencyLevel = Literal["eventual", "strong"]

# Consistency level constants
DAPR_CONSISTENCY_EVENTUAL: ConsistencyLevel = "eventual"
DAPR_CONSISTENCY_STRONG: ConsistencyLevel = "strong"

_MAX_WRITE_ATTEMPTS: Final[int] = 5
_RETRY_BASE_DELAY_SECONDS: Final[float] = 0.05
_RETRY_MAX_DELAY_SECONDS: Final[float] = 1.0


class DaprSession(SessionABC):
    """Dapr State Store implementation of [`Session`][agents.memory.session.Session]."""

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        state_store_name: str,
        dapr_client: DaprClient,
        ttl: int | None = None,
        consistency: ConsistencyLevel = DAPR_CONSISTENCY_EVENTUAL,
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ):
        """Initializes a new DaprSession.

        Args:
            session_id (str): Unique identifier for the conversation.
            state_store_name (str): Name of the Dapr state store component.
            dapr_client (DaprClient): A pre-configured Dapr client.
            ttl (int | None, optional): Time-to-live in seconds for session data.
                If None, data persists indefinitely. Note that TTL support depends on
                the underlying state store implementation. Defaults to None.
            consistency (ConsistencyLevel, optional): Consistency level for state operations.
                Use DAPR_CONSISTENCY_EVENTUAL or DAPR_CONSISTENCY_STRONG constants.
                Defaults to DAPR_CONSISTENCY_EVENTUAL.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
        """
        self.session_id = session_id
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        self._dapr_client = dapr_client
        self._state_store_name = state_store_name
        self._ttl = ttl
        self._consistency = consistency
        self._lock = asyncio.Lock()
        self._owns_client = False  # Track if we own the Dapr client
        self._closed = False
        self._client_released = False

        # State keys
        self._messages_key = f"{self.session_id}:messages"
        self._metadata_key = f"{self.session_id}:metadata"

    @classmethod
    def from_address(
        cls,
        session_id: str,
        *,
        state_store_name: str,
        dapr_address: str = "localhost:50001",
        session_settings: SessionSettings | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DaprSession:
        """Create a session from a Dapr sidecar address.

        Args:
            session_id (str): Conversation ID.
            state_store_name (str): Name of the Dapr state store component.
            dapr_address (str): Dapr sidecar gRPC address. Defaults to "localhost:50001".
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., ttl, consistency).

        Returns:
            DaprSession: An instance of DaprSession connected to the specified Dapr sidecar.

        Note:
            The Dapr Python SDK performs health checks on the HTTP endpoint (default: http://localhost:3500).
            Ensure the Dapr sidecar is started with --dapr-http-port 3500. Alternatively, set one of
            these environment variables: DAPR_HTTP_ENDPOINT (e.g., "http://localhost:3500") or
            DAPR_HTTP_PORT (e.g., "3500") to avoid connection errors.
        """
        dapr_client = DaprClient(address=dapr_address)
        session = cls(
            session_id,
            state_store_name=state_store_name,
            dapr_client=dapr_client,
            session_settings=session_settings,
            **kwargs,
        )
        session._owns_client = True  # We created the client, so we own it
        return session

    def _get_read_metadata(self) -> dict[str, str]:
        """Get metadata for read operations including consistency.

        The consistency level is passed through state_metadata as per Dapr's state API.
        """
        metadata: dict[str, str] = {}
        # Add consistency level to metadata for read operations
        if self._consistency:
            metadata["consistency"] = self._consistency
        return metadata

    def _get_state_options(self, *, concurrency: Concurrency | None = None) -> StateOptions | None:
        """Get StateOptions configured with consistency and optional concurrency."""
        options_kwargs: dict[str, Any] = {}
        if self._consistency == DAPR_CONSISTENCY_STRONG:
            options_kwargs["consistency"] = Consistency.strong
        elif self._consistency == DAPR_CONSISTENCY_EVENTUAL:
            options_kwargs["consistency"] = Consistency.eventual
        if concurrency is not None:
            options_kwargs["concurrency"] = concurrency
        if options_kwargs:
            return StateOptions(**options_kwargs)
        return None

    def _get_metadata(self) -> dict[str, str]:
        """Get metadata for state operations including TTL if configured."""
        metadata = {}
        if self._ttl is not None:
            metadata["ttlInSeconds"] = str(self._ttl)
        return metadata

    async def _read_created_at(self) -> tuple[str | None, str | None]:
        """Return the stored creation timestamp and the metadata etag backing it.

        The timestamp is None when it is missing or unreadable. The etag is returned so the
        caller can write the metadata conditionally against the same revision it read. The
        Dapr SDK reports a missing etag as an empty string, so it is normalized to None and
        callers can test for a real etag rather than for a particular empty representation.
        """
        response = await self._dapr_client.get_state(
            store_name=self._state_store_name,
            key=self._metadata_key,
            state_metadata=self._get_read_metadata(),
        )
        etag = response.etag or None
        data = response.data
        if not data:
            return None, etag
        try:
            stored = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, etag
        if not isinstance(stored, dict):
            return None, etag
        created_at = stored.get("created_at")
        return (created_at if isinstance(created_at, str) and created_at else None), etag

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, item: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(item)  # type: ignore[no-any-return]

    def _decode_messages(self, data: bytes | None, *, strict: bool = False) -> list[Any]:
        if not data:
            return []
        try:
            messages_json = data.decode("utf-8")
            messages = json.loads(messages_json)
            if isinstance(messages, list):
                return list(messages)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            if strict:
                raise ValueError(
                    "The stored Dapr session messages are not valid JSON and cannot be "
                    "safely updated."
                ) from error
            return []
        if strict:
            raise ValueError(
                "The stored Dapr session messages must be a JSON list and cannot be safely updated."
            )
        return []

    def _decode_messages_for_update(self, data: bytes | None) -> list[Any]:
        """Decode aggregate state before an operation that rewrites it."""
        return self._decode_messages(data, strict=True)

    def _calculate_retry_delay(self, attempt: int) -> float:
        base: float = _RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt - 1))
        delay: float = min(base, _RETRY_MAX_DELAY_SECONDS)
        # Add jitter (10%) similar to tracing processors to avoid thundering herd.
        return delay + random.uniform(0, 0.1 * delay)

    def _is_concurrency_conflict(self, error: Exception) -> bool:
        code_attr = getattr(error, "code", None)
        if callable(code_attr):
            try:
                status_code = code_attr()
            except Exception:
                status_code = None
            if status_code is not None:
                status_name = getattr(status_code, "name", str(status_code))
                if status_name in {"ABORTED", "FAILED_PRECONDITION"}:
                    return True
        message = str(error).lower()
        conflict_markers = (
            "etag mismatch",
            "etag does not match",
            "precondition failed",
            "concurrency conflict",
            "invalid etag",
            "failed to set key",  # Redis state store Lua script error during conditional write
            "user_script",  # Redis script failure hint
        )
        return any(marker in message for marker in conflict_markers)

    async def _handle_concurrency_conflict(self, error: Exception, attempt: int) -> bool:
        if not self._is_concurrency_conflict(error):
            return False
        if attempt >= _MAX_WRITE_ATTEMPTS:
            return False
        delay = self._calculate_retry_delay(attempt)
        if delay > 0:
            await asyncio.sleep(delay)
        return True

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    def _check_not_closed(self) -> None:
        """Raise if the session has already been closed."""
        if self._closed:
            raise RuntimeError("DaprSession is closed")

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        async with self._lock:
            self._check_not_closed()
            # Get messages from state store with consistency level
            response = await self._dapr_client.get_state(
                store_name=self._state_store_name,
                key=self._messages_key,
                state_metadata=self._get_read_metadata(),
            )

            messages = self._decode_messages(response.data)
            if not messages:
                return []
            if session_limit is not None:
                if session_limit <= 0:
                    return []
                # Walk back from the newest entry so limit counts valid conversation items:
                # a corrupt entry is skipped instead of spending the caller's budget, matching
                # pop_item and the other session backends.
                latest_first: list[TResponseInputItem] = []
                for msg in reversed(messages):
                    try:
                        if isinstance(msg, str):
                            item = await self._deserialize_item(msg)
                        else:
                            item = msg
                    except (json.JSONDecodeError, TypeError):
                        continue
                    latest_first.append(item)
                    if len(latest_first) == session_limit:
                        break
                latest_first.reverse()
                return latest_first
            items: list[TResponseInputItem] = []
            for msg in messages:
                try:
                    if isinstance(msg, str):
                        item = await self._deserialize_item(msg)
                    else:
                        item = msg
                    items.append(item)
                except (json.JSONDecodeError, TypeError):
                    continue
            return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        self._check_not_closed()
        if not items:
            return

        async with self._lock:
            self._check_not_closed()
            serialized_items: list[str] = [await self._serialize_item(item) for item in items]
            attempt = 0
            while True:
                attempt += 1
                response = await self._dapr_client.get_state(
                    store_name=self._state_store_name,
                    key=self._messages_key,
                    state_metadata=self._get_read_metadata(),
                )
                existing_messages = self._decode_messages_for_update(response.data)
                updated_messages = existing_messages + serialized_items
                messages_json = json.dumps(updated_messages, separators=(",", ":"))
                etag = response.etag
                try:
                    await self._dapr_client.save_state(
                        store_name=self._state_store_name,
                        key=self._messages_key,
                        value=messages_json,
                        etag=etag,
                        state_metadata=self._get_metadata(),
                        options=self._get_state_options(concurrency=Concurrency.first_write),
                    )
                    break
                except Exception as error:
                    should_retry = await self._handle_concurrency_conflict(error, attempt)
                    if should_retry:
                        continue
                    raise

            # Update metadata, preserving created_at across subsequent writes. A plain write
            # would let a later append overwrite created_at with its own now, so the save is
            # guarded by the etag that backed the value that was read.
            #
            # The guard only applies once a real etag was read. Dapr documents a write without
            # an etag as last-write-wins even when first-write concurrency is requested, so
            # asking for it on the create is not a race guarantee and is left off rather than
            # implying one. Concurrent etag-less creation is therefore last-write-wins.
            #
            # The messages key is already committed once we reach here, so raising would tell
            # the caller the append failed while their items are in the session, and the
            # natural response of retrying add_items() would store the batch twice. Metadata
            # is derived bookkeeping, so give up on it with a warning instead.
            attempt = 0
            while True:
                attempt += 1
                try:
                    stored_created_at, metadata_etag = await self._read_created_at()
                    now = str(int(time.time()))
                    metadata = {
                        "session_id": self.session_id,
                        "created_at": stored_created_at or now,
                        "updated_at": now,
                    }
                    await self._dapr_client.save_state(
                        store_name=self._state_store_name,
                        key=self._metadata_key,
                        value=json.dumps(metadata),
                        etag=metadata_etag,
                        state_metadata=self._get_metadata(),
                        options=self._get_state_options(
                            concurrency=(
                                Concurrency.first_write if metadata_etag is not None else None
                            )
                        ),
                    )
                    break
                except Exception as error:
                    should_retry = await self._handle_concurrency_conflict(error, attempt)
                    if should_retry:
                        continue
                    log_model_and_tool_action_warning(
                        logger,
                        "DaprSession stored the new items but could not update the session "
                        "metadata",
                        error,
                        diagnostic_extra=lambda: {"session_id": self.session_id},
                    )
                    break

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        async with self._lock:
            self._check_not_closed()
            while True:
                attempt = 0
                while True:
                    attempt += 1
                    response = await self._dapr_client.get_state(
                        store_name=self._state_store_name,
                        key=self._messages_key,
                        state_metadata=self._get_read_metadata(),
                    )
                    messages = self._decode_messages(response.data)
                    if not messages:
                        return None
                    last_item = messages.pop()
                    messages_json = json.dumps(messages, separators=(",", ":"))
                    etag = getattr(response, "etag", None) or None
                    try:
                        await self._dapr_client.save_state(
                            store_name=self._state_store_name,
                            key=self._messages_key,
                            value=messages_json,
                            etag=etag,
                            state_metadata=self._get_metadata(),
                            options=self._get_state_options(concurrency=Concurrency.first_write),
                        )
                        break
                    except Exception as error:
                        should_retry = await self._handle_concurrency_conflict(error, attempt)
                        if should_retry:
                            continue
                        raise
                try:
                    if isinstance(last_item, str):
                        return await self._deserialize_item(last_item)
                    return last_item  # type: ignore[no-any-return]
                except (json.JSONDecodeError, TypeError):
                    continue

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            self._check_not_closed()
            # Delete messages and metadata keys
            await self._dapr_client.delete_state(
                store_name=self._state_store_name,
                key=self._messages_key,
                options=self._get_state_options(),
            )

            await self._dapr_client.delete_state(
                store_name=self._state_store_name,
                key=self._metadata_key,
                options=self._get_state_options(),
            )

    async def close(self) -> None:
        """Close the Dapr client connection.

        Only closes the connection if this session owns the Dapr client
        (i.e., created via from_address). In that case the session becomes
        terminal and subsequent operations raise RuntimeError. If the client was
        injected externally, the caller is responsible for managing its lifecycle
        and this is a no-op.

        The session is terminal from the first close attempt. If releasing the
        client fails or is cancelled, operations still raise and a later close()
        retries the unfinished cleanup. Once the client is released, repeated and
        concurrent calls are safe no-ops.
        """
        async with self._lock:
            if not self._owns_client:
                return
            self._closed = True
            if not self._client_released:
                await self._dapr_client.close()
                self._client_released = True

    async def __aenter__(self) -> DaprSession:
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager and close the connection."""
        await self.close()

    async def ping(self) -> bool:
        """Test Dapr connectivity by checking metadata.

        Returns:
            True if Dapr is reachable, False otherwise.

        Raises:
            RuntimeError: If the session owns its client and has been closed.
        """
        async with self._lock:
            # Checked outside the try block; the except clause below would swallow it.
            self._check_not_closed()
            try:
                # First attempt a read; some stores may not be initialized yet.
                await self._dapr_client.get_state(
                    store_name=self._state_store_name,
                    key="__ping__",
                    state_metadata=self._get_read_metadata(),
                )
                return True
            except Exception as initial_error:
                # If relation/table is missing or store isn't initialized,
                # attempt a write to initialize it, then read again.
                try:
                    await self._dapr_client.save_state(
                        store_name=self._state_store_name,
                        key="__ping__",
                        value="ok",
                        state_metadata=self._get_metadata(),
                        options=self._get_state_options(),
                    )
                    # Read again after write.
                    await self._dapr_client.get_state(
                        store_name=self._state_store_name,
                        key="__ping__",
                        state_metadata=self._get_read_metadata(),
                    )
                    return True
                except Exception:
                    log_model_and_tool_action_error(logger, "Dapr connection failed", initial_error)
                    return False
