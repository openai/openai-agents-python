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
import time
from typing import Any, Literal

try:
    from dapr.aio.clients import DaprClient
    from dapr.clients.grpc._state import Consistency, StateOptions
except ImportError as e:
    raise ImportError(
        "DaprSession requires the 'dapr' package. Install it with: pip install dapr"
    ) from e

from ...items import TResponseInputItem
from ...memory.session import SessionABC

# Type alias for consistency levels
ConsistencyLevel = Literal["eventual", "strong"]

# Consistency level constants
DAPR_CONSISTENCY_EVENTUAL: ConsistencyLevel = "eventual"
DAPR_CONSISTENCY_STRONG: ConsistencyLevel = "strong"


class DaprSession(SessionABC):
    """Dapr State Store implementation of :pyclass:`agents.memory.session.Session`."""

    def __init__(
        self,
        session_id: str,
        *,
        state_store_name: str,
        dapr_client: DaprClient,
        ttl: int | None = None,
        consistency: ConsistencyLevel = DAPR_CONSISTENCY_EVENTUAL,
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
                Use CONSISTENCY_EVENTUAL or CONSISTENCY_STRONG constants.
                Defaults to CONSISTENCY_EVENTUAL.
        """
        self.session_id = session_id
        self._dapr_client = dapr_client
        self._state_store_name = state_store_name
        self._ttl = ttl
        self._consistency = consistency
        self._lock = asyncio.Lock()
        self._owns_client = False  # Track if we own the Dapr client

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
        **kwargs: Any,
    ) -> DaprSession:
        """Create a session from a Dapr sidecar address.

        Args:
            session_id (str): Conversation ID.
            state_store_name (str): Name of the Dapr state store component.
            dapr_address (str): Dapr sidecar gRPC address. Defaults to "localhost:50001".
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
            session_id, state_store_name=state_store_name, dapr_client=dapr_client, **kwargs
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

    def _get_state_options(self) -> StateOptions | None:
        """Get StateOptions for write/delete consistency level."""
        if self._consistency == DAPR_CONSISTENCY_STRONG:
            return StateOptions(consistency=Consistency.strong)
        elif self._consistency == DAPR_CONSISTENCY_EVENTUAL:
            return StateOptions(consistency=Consistency.eventual)
        return None

    def _get_metadata(self) -> dict[str, str]:
        """Get metadata for state operations including TTL if configured."""
        metadata = {}
        if self._ttl is not None:
            metadata["ttlInSeconds"] = str(self._ttl)
        return metadata

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, item: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(item)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        async with self._lock:
            # Get messages from state store with consistency level
            response = await self._dapr_client.get_state(
                store_name=self._state_store_name,
                key=self._messages_key,
                state_metadata=self._get_read_metadata(),
            )

            if not response.data:
                return []

            try:
                # Parse the messages list from JSON
                messages_json = response.data.decode("utf-8")
                messages = json.loads(messages_json)

                if not isinstance(messages, list):
                    return []

                # Apply limit if specified
                if limit is not None:
                    if limit <= 0:
                        return []
                    # Return the latest N items
                    messages = messages[-limit:]

                items: list[TResponseInputItem] = []
                for msg in messages:
                    try:
                        if isinstance(msg, str):
                            item = await self._deserialize_item(msg)
                        else:
                            item = msg  # Already deserialized
                        items.append(item)
                    except (json.JSONDecodeError, TypeError):
                        # Skip corrupted messages
                        continue

                return items

            except (json.JSONDecodeError, UnicodeDecodeError):
                # Return empty list for corrupted data
                return []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        async with self._lock:
            # Get existing messages with consistency level
            response = await self._dapr_client.get_state(
                store_name=self._state_store_name,
                key=self._messages_key,
                state_metadata=self._get_read_metadata(),
            )

            # Parse existing messages
            existing_messages = []
            if response.data:
                try:
                    messages_json = response.data.decode("utf-8")
                    existing_messages = json.loads(messages_json)
                    if not isinstance(existing_messages, list):
                        existing_messages = []
                except (json.JSONDecodeError, UnicodeDecodeError):
                    existing_messages = []

            # Serialize and append new items
            for item in items:
                serialized = await self._serialize_item(item)
                existing_messages.append(serialized)

            # Save updated messages list
            messages_json = json.dumps(existing_messages, separators=(",", ":"))
            await self._dapr_client.save_state(
                store_name=self._state_store_name,
                key=self._messages_key,
                value=messages_json,
                state_metadata=self._get_metadata(),
                options=self._get_state_options(),
            )

            # Update metadata
            metadata = {
                "session_id": self.session_id,
                "created_at": str(int(time.time())),
                "updated_at": str(int(time.time())),
            }
            await self._dapr_client.save_state(
                store_name=self._state_store_name,
                key=self._metadata_key,
                value=json.dumps(metadata),
                state_metadata=self._get_metadata(),
                options=self._get_state_options(),
            )

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        async with self._lock:
            # Get messages from state store with consistency level
            response = await self._dapr_client.get_state(
                store_name=self._state_store_name,
                key=self._messages_key,
                state_metadata=self._get_read_metadata(),
            )

            if not response.data:
                return None

            try:
                # Parse the messages list
                messages_json = response.data.decode("utf-8")
                messages = json.loads(messages_json)

                if not isinstance(messages, list) or len(messages) == 0:
                    return None

                # Pop the last item
                last_item = messages.pop()

                # Save updated messages list
                messages_json = json.dumps(messages, separators=(",", ":"))
                await self._dapr_client.save_state(
                    store_name=self._state_store_name,
                    key=self._messages_key,
                    value=messages_json,
                    state_metadata=self._get_metadata(),
                    options=self._get_state_options(),
                )

                # Deserialize and return the item
                if isinstance(last_item, str):
                    return await self._deserialize_item(last_item)
                else:
                    return last_item  # type: ignore[no-any-return]

            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                # Return None for corrupted data
                return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
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
        (i.e., created via from_address). If the client was injected externally,
        the caller is responsible for managing its lifecycle.
        """
        if self._owns_client:
            await self._dapr_client.close()

    async def ping(self) -> bool:
        """Test Dapr connectivity by checking metadata.

        Returns:
            True if Dapr is reachable, False otherwise.
        """
        try:
            # Try to get state with a test key
            await self._dapr_client.get_state(
                store_name=self._state_store_name,
                key="__ping__",
            )
            return True
        except Exception:
            return False
