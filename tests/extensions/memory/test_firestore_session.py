"""Tests for FirestoreSession using in-process mock objects.

All tests run without a real Firestore server — or even the
``google-cloud-firestore`` package — by injecting lightweight fake classes
into ``sys.modules`` before the module under test is imported.  This keeps
the suite fast and dependency-free while exercising the full session logic.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from agents import Agent, Runner, TResponseInputItem
from agents.memory.session_settings import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# In-memory fake Firestore async types
# ---------------------------------------------------------------------------


class FakeDocumentSnapshot:
    """Minimal stand-in for a Firestore DocumentSnapshot."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data
        self.exists = data is not None

    def get(self, field: str, default: Any = None) -> Any:
        if self._data is None:
            return default
        return self._data.get(field, default)

    def to_dict(self) -> dict[str, Any] | None:
        return self._data


class FakeDocumentReference:
    """In-memory substitute for a Firestore AsyncDocumentReference."""

    def __init__(
        self,
        store: dict[str, Any],
        col_registry: dict[str, FakeCollectionReference],
        path: str,
    ) -> None:
        self._store = store
        self._col_registry = col_registry
        self._path = path
        self._subcollections: dict[str, FakeCollectionReference] = {}

    def collection(self, name: str) -> FakeCollectionReference:
        key = f"{self._path}/{name}"
        if key not in self._subcollections:
            col = FakeCollectionReference(self._store, self._col_registry, key)
            self._subcollections[key] = col
            self._col_registry[key] = col
        return self._subcollections[key]

    async def get(self, transaction: Any = None) -> FakeDocumentSnapshot:
        return FakeDocumentSnapshot(self._store.get(self._path))

    async def set(self, data: dict[str, Any]) -> None:
        self._store[self._path] = dict(data)
        # Register in parent collection
        parts = self._path.rsplit("/", 1)
        if len(parts) == 2:
            col_path, doc_id = parts
            col = self._col_registry.get(col_path)
            if col is not None:
                col._docs[doc_id] = FakeQueryDocumentSnapshot(self, dict(data))

    async def update(self, data: dict[str, Any]) -> None:
        existing = self._store.get(self._path, {})
        existing.update(data)
        self._store[self._path] = existing

    async def delete(self) -> None:
        self._store.pop(self._path, None)
        parts = self._path.rsplit("/", 1)
        if len(parts) == 2:
            col_path, doc_id = parts
            col = self._col_registry.get(col_path)
            if col is not None:
                col._docs.pop(doc_id, None)


class FakeQueryDocumentSnapshot:
    """Minimal stand-in for a query result document."""

    def __init__(self, ref: FakeDocumentReference, data: dict[str, Any]) -> None:
        self.reference = ref
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class FakeQuery:
    """Minimal async query that supports order_by, limit, and stream."""

    def __init__(
        self,
        collection: FakeCollectionReference,
        order_field: str | None = None,
        order_desc: bool = False,
        limit_n: int | None = None,
    ) -> None:
        self._collection = collection
        self._order_field = order_field
        self._order_desc = order_desc
        self._limit_n = limit_n

    def order_by(self, field: str, direction: str = "ASCENDING") -> FakeQuery:
        return FakeQuery(
            self._collection,
            order_field=field,
            order_desc=(direction == "DESCENDING"),
            limit_n=self._limit_n,
        )

    def limit(self, n: int) -> FakeQuery:
        return FakeQuery(
            self._collection,
            order_field=self._order_field,
            order_desc=self._order_desc,
            limit_n=n,
        )

    def stream(self) -> FakeAsyncIterator:
        docs = list(self._collection._docs.values())
        if self._order_field:
            docs.sort(
                key=lambda d: d._data.get(self._order_field, 0),
                reverse=self._order_desc,
            )
        if self._limit_n is not None:
            docs = docs[: self._limit_n]
        return FakeAsyncIterator(docs)


class FakeAsyncIterator:
    """Async iterator over a list of documents."""

    def __init__(self, docs: list[FakeQueryDocumentSnapshot]) -> None:
        self._docs = iter(docs)

    def __aiter__(self) -> FakeAsyncIterator:
        return self

    async def __anext__(self) -> FakeQueryDocumentSnapshot:
        try:
            return next(self._docs)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeCollectionReference:
    """In-memory substitute for a Firestore AsyncCollectionReference."""

    def __init__(
        self,
        store: dict[str, Any],
        col_registry: dict[str, FakeCollectionReference],
        path: str,
    ) -> None:
        self._store = store
        self._col_registry = col_registry
        self._path = path
        self._docs: dict[str, FakeQueryDocumentSnapshot] = {}
        self._counter = 0

    def document(self, doc_id: str | None = None) -> FakeDocumentReference:
        if doc_id is None:
            self._counter += 1
            doc_id = f"auto_{self._counter}"
        return FakeDocumentReference(self._store, self._col_registry, f"{self._path}/{doc_id}")

    def order_by(self, field: str, direction: str = "ASCENDING") -> FakeQuery:
        return FakeQuery(self, order_field=field, order_desc=(direction == "DESCENDING"))

    def limit(self, n: int) -> FakeQuery:
        return FakeQuery(self, limit_n=n)

    def stream(self) -> FakeAsyncIterator:
        return FakeAsyncIterator(list(self._docs.values()))


class FakeBatch:
    """In-memory substitute for a Firestore WriteBatch."""

    def __init__(self, col_registry: dict[str, FakeCollectionReference]) -> None:
        self._ops: list[tuple[str, FakeDocumentReference, dict[str, Any] | None]] = []
        self._registry = col_registry

    def set(self, ref: FakeDocumentReference, data: dict[str, Any]) -> None:
        self._ops.append(("set", ref, data))

    def delete(self, ref: FakeDocumentReference) -> None:
        self._ops.append(("delete", ref, None))

    async def commit(self) -> None:
        for op, ref, data in self._ops:
            if op == "set" and data is not None:
                ref._store[ref._path] = dict(data)
                parts = ref._path.rsplit("/", 1)
                if len(parts) == 2:
                    col_path, doc_id = parts
                    col = self._registry.get(col_path)
                    if col is not None:
                        col._docs[doc_id] = FakeQueryDocumentSnapshot(ref, dict(data))
            elif op == "delete":
                ref._store.pop(ref._path, None)
                parts = ref._path.rsplit("/", 1)
                if len(parts) == 2:
                    col_path, doc_id = parts
                    col = self._registry.get(col_path)
                    if col is not None:
                        col._docs.pop(doc_id, None)


class FakeTransaction:
    """Minimal transaction context that executes the decorated coroutine."""

    def __init__(self, client: FakeAsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> FakeTransaction:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def update(self, ref: FakeDocumentReference, data: dict[str, Any]) -> None:
        existing = ref._store.get(ref._path, {})
        existing.update(data)
        ref._store[ref._path] = existing

    def set(self, ref: FakeDocumentReference, data: dict[str, Any]) -> None:
        ref._store[ref._path] = dict(data)


class FakeAsyncClient:
    """In-memory substitute for google.cloud.firestore_v1.async_client.AsyncClient."""

    def __init__(self, **kwargs: Any) -> None:
        self._store: dict[str, Any] = {}
        self._collections: dict[str, FakeCollectionReference] = {}

    def collection(self, name: str) -> FakeCollectionReference:
        if name not in self._collections:
            self._collections[name] = FakeCollectionReference(self._store, self._collections, name)
        return self._collections[name]

    def batch(self) -> FakeBatch:
        return FakeBatch(self._collections)

    def transaction(self) -> Any:
        """Return a decorator that wraps the coroutine with transaction semantics."""
        txn = FakeTransaction(self)

        def decorator(coro_fn: Any) -> Any:
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                return await coro_fn(txn, *args, **kwargs)

            return wrapper

        return decorator

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixture: inject fake google-cloud-firestore into sys.modules
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_firestore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace google-cloud-firestore with in-memory fakes before each test."""
    # Build a minimal module tree that satisfies the imports in firestore_session.py.
    google = types.ModuleType("google")
    google_cloud = types.ModuleType("google.cloud")
    firestore_pkg = types.ModuleType("google.cloud.firestore_v1")
    async_client_mod = types.ModuleType("google.cloud.firestore_v1.async_client")
    async_collection_mod = types.ModuleType("google.cloud.firestore_v1.async_collection")
    async_document_mod = types.ModuleType("google.cloud.firestore_v1.async_document")
    base_query_mod = types.ModuleType("google.cloud.firestore_v1.base_query")

    async_client_mod.AsyncClient = FakeAsyncClient  # type: ignore[attr-defined]
    async_collection_mod.AsyncCollectionReference = FakeCollectionReference  # type: ignore[attr-defined]
    async_document_mod.AsyncDocumentReference = FakeDocumentReference  # type: ignore[attr-defined]
    base_query_mod.FieldFilter = object  # type: ignore[attr-defined]

    google.cloud = google_cloud  # type: ignore[attr-defined]
    google_cloud.firestore_v1 = firestore_pkg  # type: ignore[attr-defined]

    for mod_name, mod in [
        ("google", google),
        ("google.cloud", google_cloud),
        ("google.cloud.firestore_v1", firestore_pkg),
        ("google.cloud.firestore_v1.async_client", async_client_mod),
        ("google.cloud.firestore_v1.async_collection", async_collection_mod),
        ("google.cloud.firestore_v1.async_document", async_document_mod),
        ("google.cloud.firestore_v1.base_query", base_query_mod),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    # Force re-import of the module under test so it picks up the fakes.
    monkeypatch.delitem(
        sys.modules,
        "agents.extensions.memory.firestore_session",
        raising=False,
    )


def make_session(session_id: str = "test-session", **kwargs: Any):  # type: ignore[no-untyped-def]
    from agents.extensions.memory.firestore_session import FirestoreSession

    client = FakeAsyncClient()
    return FirestoreSession(session_id=session_id, client=client, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_empty_session_returns_no_items() -> None:
    session = make_session()
    items = await session.get_items()
    assert items == []


async def test_add_and_get_items() -> None:
    session = make_session()
    msg: TResponseInputItem = {"role": "user", "content": "hello"}
    await session.add_items([msg])
    items = await session.get_items()
    assert len(items) == 1
    assert items[0] == msg


async def test_items_returned_in_chronological_order() -> None:
    session = make_session()
    msgs: list[TResponseInputItem] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    await session.add_items(msgs)
    items = await session.get_items()
    assert [i["content"] for i in items] == ["first", "second", "third"]


async def test_get_items_with_limit() -> None:
    session = make_session()
    msgs: list[TResponseInputItem] = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
    await session.add_items(msgs)
    items = await session.get_items(limit=3)
    assert len(items) == 3
    assert items[0]["content"] == "msg2"
    assert items[-1]["content"] == "msg4"


async def test_get_items_limit_zero_returns_empty() -> None:
    session = make_session()
    await session.add_items([{"role": "user", "content": "hi"}])
    items = await session.get_items(limit=0)
    assert items == []


async def test_pop_item_returns_most_recent() -> None:
    session = make_session()
    msgs: list[TResponseInputItem] = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    await session.add_items(msgs)
    popped = await session.pop_item()
    assert popped is not None
    assert popped["content"] == "second"
    remaining = await session.get_items()
    assert len(remaining) == 1
    assert remaining[0]["content"] == "first"


async def test_pop_item_on_empty_session_returns_none() -> None:
    session = make_session()
    result = await session.pop_item()
    assert result is None


async def test_clear_session() -> None:
    session = make_session()
    await session.add_items([{"role": "user", "content": "hi"}])
    await session.clear_session()
    items = await session.get_items()
    assert items == []


async def test_multiple_add_calls_preserve_order() -> None:
    session = make_session()
    await session.add_items([{"role": "user", "content": "a"}])
    await session.add_items([{"role": "assistant", "content": "b"}])
    await session.add_items([{"role": "user", "content": "c"}])
    items = await session.get_items()
    assert [i["content"] for i in items] == ["a", "b", "c"]


async def test_session_settings_limit() -> None:
    session = make_session(session_settings=SessionSettings(limit=2))
    msgs: list[TResponseInputItem] = [{"role": "user", "content": f"msg{i}"} for i in range(4)]
    await session.add_items(msgs)
    items = await session.get_items()
    assert len(items) == 2
    assert items[0]["content"] == "msg2"
    assert items[1]["content"] == "msg3"


async def test_add_empty_list_is_noop() -> None:
    session = make_session()
    await session.add_items([])
    items = await session.get_items()
    assert items == []


async def test_close_owned_client() -> None:
    from agents.extensions.memory.firestore_session import FirestoreSession

    client = FakeAsyncClient()
    session = FirestoreSession(session_id="s", client=client)
    session._owns_client = True
    # Should not raise.
    await session.close()


async def test_close_unowned_client_does_not_close() -> None:
    from agents.extensions.memory.firestore_session import FirestoreSession

    client = FakeAsyncClient()
    closed = False
    original_close = client.close

    async def tracking_close() -> None:
        nonlocal closed
        closed = True
        await original_close()

    client.close = tracking_close  # type: ignore[method-assign]
    session = FirestoreSession(session_id="s", client=client)
    # _owns_client defaults to False
    await session.close()
    assert not closed


async def test_runner_integration() -> None:
    """Smoke-test: FirestoreSession works end-to-end with Runner."""

    session = make_session(session_id="runner-test")
    model = FakeModel(initial_output=[get_text_message("Hello!")])
    agent = Agent(name="test", model=model)

    result = await Runner.run(agent, "Hi", session=session)
    assert result.final_output == "Hello!"

    items = await session.get_items()
    assert len(items) > 0
