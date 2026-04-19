"""Google Cloud Firestore-powered Session backend.

Requires ``google-cloud-firestore>=2.19``, which ships the native async API.
Install it with::

    pip install openai-agents[firestore]

Usage::

    from agents.extensions.memory import FirestoreSession

    # Create from a Google Cloud project ID (uses Application Default Credentials)
    session = FirestoreSession.from_project(
        session_id="user-123",
        project="my-gcp-project",
    )

    # Or pass an existing AsyncClient that your application already manages
    from google.cloud.firestore_v1.async_client import AsyncClient

    client = AsyncClient(project="my-gcp-project")
    session = FirestoreSession(
        session_id="user-123",
        client=client,
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

try:
    from google.cloud.firestore_v1.async_client import AsyncClient
    from google.cloud.firestore_v1.async_collection import AsyncCollectionReference
    from google.cloud.firestore_v1.async_document import AsyncDocumentReference
except ImportError as e:
    raise ImportError(
        "FirestoreSession requires the 'google-cloud-firestore' package (>=2.19). "
        "Install it with: pip install openai-agents[firestore]"
    ) from e

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings, resolve_session_limit


class FirestoreSession(SessionABC):
    """Google Cloud Firestore implementation of :class:`agents.memory.session.Session`.

    Conversation items are stored as individual documents in a ``messages``
    sub-collection under each session document.  A parent ``sessions``
    collection holds lightweight metadata (creation time, last-updated time,
    and a monotonic sequence counter) for each session.

    Each message document carries a ``seq`` field — an integer assigned by
    atomically incrementing a counter on the session metadata document via a
    Firestore transaction.  This guarantees a strictly monotonic insertion
    order that is safe across multiple writers and processes.

    Data layout in Firestore::

        {sessions_collection}/
          {session_id}                    ← session metadata doc
            _seq: int                     ← monotonic counter
            created_at: int               ← Unix timestamp
            updated_at: int               ← Unix timestamp
            messages/                     ← sub-collection
              {auto_id}
                seq: int
                message_data: str         ← JSON-serialized TResponseInputItem
    """

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        client: AsyncClient,
        sessions_collection: str = "agent_sessions",
        session_settings: SessionSettings | None = None,
    ):
        """Initialize a new FirestoreSession.

        Args:
            session_id: Unique identifier for the conversation.
            client: A pre-configured Firestore :class:`AsyncClient` instance.
            sessions_collection: Name of the top-level Firestore collection that
                stores session documents.  Each session document contains a
                ``messages`` sub-collection.  Defaults to ``"agent_sessions"``.
            session_settings: Optional session configuration.  When ``None`` a
                default :class:`~agents.memory.session_settings.SessionSettings`
                is used (no item limit).
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._client = client
        self._owns_client = False
        self._lock = asyncio.Lock()

        self._session_ref: AsyncDocumentReference = client.collection(sessions_collection).document(
            session_id
        )
        self._messages_ref: AsyncCollectionReference = self._session_ref.collection("messages")

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_project(
        cls,
        session_id: str,
        *,
        project: str,
        database: str = "(default)",
        client_kwargs: dict[str, Any] | None = None,
        session_settings: SessionSettings | None = None,
        **kwargs: Any,
    ) -> FirestoreSession:
        """Create a session from a Google Cloud project ID.

        Authentication uses `Application Default Credentials`_ (ADC).  Run
        ``gcloud auth application-default login`` locally, or rely on the
        service account attached to your GCP resource in production.

        .. _Application Default Credentials:
            https://cloud.google.com/docs/authentication/application-default-credentials

        Args:
            session_id: Conversation ID.
            project: Google Cloud project ID.
            database: Firestore database ID.  Defaults to ``"(default)"``.
            client_kwargs: Additional keyword arguments forwarded to
                :class:`google.cloud.firestore_v1.async_client.AsyncClient`.
            session_settings: Optional session configuration settings.
            **kwargs: Additional keyword arguments forwarded to the main
                constructor (e.g. ``sessions_collection``).

        Returns:
            A :class:`FirestoreSession` connected to the specified project.
        """
        client_kwargs = client_kwargs or {}
        client = AsyncClient(project=project, database=database, **client_kwargs)
        session = cls(
            session_id,
            client=client,
            session_settings=session_settings,
            **kwargs,
        )
        session._owns_client = True
        return session

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to a JSON string.  Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, raw: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item.  Can be overridden by subclasses."""
        return json.loads(raw)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve.  When ``None``, the
                effective limit is taken from :attr:`session_settings`.
                If that is also ``None``, all items are returned.
                The returned list is always in chronological (oldest-first)
                order.

        Returns:
            List of input items representing the conversation history.
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        if session_limit is not None and session_limit <= 0:
            return []

        query = self._messages_ref.order_by("seq")

        if session_limit is not None:
            # Firestore has no native "last N" query; fetch all and slice.
            # For large histories consider storing a running offset in the
            # session metadata document and using a range query instead.
            docs_stream = query.stream()
            all_docs = [doc async for doc in docs_stream]
            docs = all_docs[-session_limit:]
        else:
            docs_stream = query.stream()
            docs = [doc async for doc in docs_stream]

        items: list[TResponseInputItem] = []
        for doc in docs:
            data = doc.to_dict()
            if data is None:
                continue
            try:
                items.append(await self._deserialize_item(data["message_data"]))
            except (json.JSONDecodeError, KeyError, TypeError):
                # Skip corrupted or malformed documents.
                continue

        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to append to the session.
        """
        if not items:
            return

        import time

        async with self._lock:
            # Atomically reserve a block of sequence numbers via a transaction.
            @self._client.transaction()  # type: ignore[arg-type]
            async def _txn(transaction: Any) -> int:
                snap = await self._session_ref.get(transaction=transaction)
                current_seq: int = snap.get("_seq") if snap.exists else 0  # type: ignore[union-attr]
                new_seq = current_seq + len(items)
                now = int(time.time())
                if snap.exists:
                    transaction.update(
                        self._session_ref,
                        {"_seq": new_seq, "updated_at": now},
                    )
                else:
                    transaction.set(
                        self._session_ref,
                        {
                            "_seq": new_seq,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                return current_seq

            first_seq: int = await _txn()  # type: ignore[call-arg]

            # Write message documents outside the transaction (non-atomic batch
            # is fine here — sequence numbers are already reserved).
            batch = self._client.batch()
            for i, item in enumerate(items):
                doc_ref = self._messages_ref.document()
                batch.set(
                    doc_ref,
                    {
                        "seq": first_seq + i,
                        "message_data": await self._serialize_item(item),
                    },
                )
            await batch.commit()

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, ``None`` if the session is empty.
        """
        async with self._lock:
            # Find the document with the highest seq value.
            query = self._messages_ref.order_by("seq", direction="DESCENDING").limit(1)
            docs = [doc async for doc in query.stream()]

            if not docs:
                return None

            doc = docs[0]
            data = doc.to_dict()
            await doc.reference.delete()

            if data is None:
                return None

            try:
                return await self._deserialize_item(data["message_data"])
            except (json.JSONDecodeError, KeyError, TypeError):
                return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            # Delete all message documents in batches of 500 (Firestore limit).
            batch_size = 500
            while True:
                docs = [doc async for doc in self._messages_ref.limit(batch_size).stream()]
                if not docs:
                    break
                batch = self._client.batch()
                for doc in docs:
                    batch.delete(doc.reference)
                await batch.commit()

            # Delete the session metadata document.
            await self._session_ref.delete()

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying Firestore client.

        Only closes the client if this session owns it (i.e. it was created
        via :meth:`from_project`).  If the client was injected externally the
        caller is responsible for managing its lifecycle.
        """
        if self._owns_client:
            await self._client.close()

    async def ping(self) -> bool:
        """Test Firestore connectivity.

        Returns:
            ``True`` if the service is reachable, ``False`` otherwise.
        """
        try:
            # A lightweight read against the session document is sufficient to
            # verify that the client can reach the Firestore service.
            await self._session_ref.get()
            return True
        except Exception:
            return False
