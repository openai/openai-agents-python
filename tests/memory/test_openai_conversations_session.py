"""Tests for OpenAI Conversations Session functionality."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError
from openai.types.responses.response_output_item import Program, ProgramOutput

from agents import (
    Agent,
    GuardrailFunctionOutput,
    HandoffInputData,
    MessageOutputItem,
    ProgrammaticToolCallingTool,
    RunConfig,
    Runner,
    RunState,
    TResponseInputItem,
    function_tool,
    handoff,
    output_guardrail,
)
from agents.decorators import tool
from agents.exceptions import UserError
from agents.memory.openai_conversations_session import (
    OpenAIConversationsSession,
    start_openai_conversations_session,
)
from agents.testing import ScriptedModel
from tests.test_responses import get_function_tool_call, get_text_message
from tests.utils.simple_session import IdStrippingSession


@pytest.fixture
def mock_openai_client():
    """Create a mock OpenAI client for testing."""
    client = AsyncMock()

    # Mock conversations.create
    client.conversations.create.return_value = MagicMock(id="test_conversation_id")

    # Mock conversations.delete
    client.conversations.delete.return_value = None

    # Mock conversations.items.create
    client.conversations.items.create.return_value = None

    # Mock conversations.items.delete
    client.conversations.items.delete.return_value = None

    return client


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a scripted model."""
    return Agent(name="test", model=ScriptedModel())


class TestStartOpenAIConversationsSession:
    """Test the standalone start_openai_conversations_session function."""

    @pytest.mark.asyncio
    async def test_start_with_provided_client(self, mock_openai_client):
        """Test starting a conversation session with a provided client."""
        conversation_id = await start_openai_conversations_session(mock_openai_client)

        assert conversation_id == "test_conversation_id"
        mock_openai_client.conversations.create.assert_called_once_with(items=[])

    @pytest.mark.asyncio
    async def test_start_with_none_client(self):
        """Test starting a conversation session with None client (uses default)."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch("agents.memory.openai_conversations_session.AsyncOpenAI"):
                # Test case 1: get_default_openai_client returns a client
                mock_default_client = AsyncMock()
                mock_default_client.conversations.create.return_value = MagicMock(
                    id="default_client_id"
                )
                mock_get_default.return_value = mock_default_client

                conversation_id = await start_openai_conversations_session(None)

                assert conversation_id == "default_client_id"
                mock_get_default.assert_called_once()
                mock_default_client.conversations.create.assert_called_once_with(items=[])

    @pytest.mark.asyncio
    async def test_start_preserves_falsy_default_client(self):
        mock_default_client = AsyncMock()
        mock_default_client.__bool__.return_value = False
        mock_default_client.conversations.create.return_value = MagicMock(id="default_client_id")

        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client",
            return_value=mock_default_client,
        ):
            conversation_id = await start_openai_conversations_session(None)

        assert conversation_id == "default_client_id"
        mock_default_client.conversations.create.assert_awaited_once_with(items=[])

    @pytest.mark.asyncio
    async def test_start_with_none_client_fallback(self):
        """Test starting a conversation session when get_default_openai_client returns None."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch(
                "agents.memory.openai_conversations_session.AsyncOpenAI"
            ) as mock_async_openai:
                # Test case 2: get_default_openai_client returns None, fallback to AsyncOpenAI()
                mock_get_default.return_value = None
                mock_fallback_client = AsyncMock()
                mock_fallback_client.conversations.create.return_value = MagicMock(
                    id="fallback_client_id"
                )
                mock_async_openai.return_value = mock_fallback_client

                conversation_id = await start_openai_conversations_session(None)

                assert conversation_id == "fallback_client_id"
                mock_get_default.assert_called_once()
                mock_async_openai.assert_called_once()
                mock_fallback_client.conversations.create.assert_called_once_with(items=[])


class TestOpenAIConversationsSessionConstructor:
    """Test OpenAIConversationsSession constructor and client handling."""

    def test_init_with_conversation_id_and_client(self, mock_openai_client):
        """Test constructor with both conversation_id and openai_client provided."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        assert session._session_id == "test_id"
        assert session._openai_client is mock_openai_client

    def test_init_with_conversation_id_only(self):
        """Test constructor with only conversation_id, client should be created."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch("agents.memory.openai_conversations_session.AsyncOpenAI"):
                mock_default_client = AsyncMock()
                mock_get_default.return_value = mock_default_client

                session = OpenAIConversationsSession(conversation_id="test_id")

                assert session._session_id == "test_id"
                assert session._openai_client is mock_default_client
                mock_get_default.assert_called_once()

    def test_init_with_client_only(self, mock_openai_client):
        """Test constructor with only openai_client, no conversation_id."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        assert session._session_id is None
        assert session._openai_client is mock_openai_client

    def test_init_with_no_args_fallback(self):
        """Test constructor with no args, should create default client."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch(
                "agents.memory.openai_conversations_session.AsyncOpenAI"
            ) as mock_async_openai:
                # Test fallback when get_default_openai_client returns None
                mock_get_default.return_value = None
                mock_fallback_client = AsyncMock()
                mock_async_openai.return_value = mock_fallback_client

                session = OpenAIConversationsSession()

                assert session._session_id is None
                assert session._openai_client is mock_fallback_client
                mock_get_default.assert_called_once()
                mock_async_openai.assert_called_once()


class TestOpenAIConversationsSessionLifecycle:
    """Test session ID lifecycle management."""

    @pytest.mark.asyncio
    async def test_get_session_id_with_existing_id(self, mock_openai_client):
        """Test _get_session_id when session_id already exists."""
        session = OpenAIConversationsSession(
            conversation_id="existing_id", openai_client=mock_openai_client
        )

        session_id = await session._get_session_id()

        assert session_id == "existing_id"
        # Should not call conversations.create since ID already exists
        mock_openai_client.conversations.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_session_id_creates_new_conversation(self, mock_openai_client):
        """Test _get_session_id when session_id is None, should create new conversation."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        session_id = await session._get_session_id()

        assert session_id == "test_conversation_id"
        assert session._session_id == "test_conversation_id"
        mock_openai_client.conversations.create.assert_called_once_with(items=[])

    @pytest.mark.asyncio
    async def test_clear_session_id(self, mock_openai_client):
        """Test _clear_session_id sets session_id to None."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        await session._clear_session_id()

        assert session._session_id is None


class TestOpenAIConversationsSessionBasicOperations:
    """Test basic CRUD operations with simple mocking."""

    @pytest.mark.asyncio
    async def test_get_items_zero_limit_returns_empty_without_api_call(self, mock_openai_client):
        """A zero history limit must not be forwarded to the Conversations API."""
        mock_openai_client.conversations.items.list = MagicMock(
            side_effect=AssertionError("items.list must not receive limit=0")
        )
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        assert await session.get_items(limit=0) == []

        mock_openai_client.conversations.create.assert_awaited_once_with(items=[])
        assert session.session_id == "test_conversation_id"
        mock_openai_client.conversations.items.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_items_applies_large_limit_after_provider_pagination(
        self, mock_openai_client
    ):
        """A session limit must not be forwarded as the Conversations API page size."""

        class ConversationItem:
            def __init__(self, item_id: int) -> None:
                self.item_id = item_id

            def model_dump(self, *, exclude_unset: bool) -> dict[str, int]:
                assert exclude_unset is True
                return {"item_id": self.item_id}

        yielded_item_ids: list[int] = []

        async def descending_items():
            # Yield one extra newest-first item so the local cutoff can prove it stops at N.
            for item_id in range(101, -1, -1):
                yielded_item_ids.append(item_id)
                yield ConversationItem(item_id)

        mock_openai_client.conversations.items.list = MagicMock(return_value=descending_items())
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        items = await session.get_items(limit=101)

        assert [cast(dict[str, int], item)["item_id"] for item in items] == list(range(1, 102))
        assert yielded_item_ids == list(range(101, 0, -1))
        mock_openai_client.conversations.items.list.assert_called_once_with(
            conversation_id="test_id", order="desc"
        )

    @pytest.mark.asyncio
    async def test_add_items_simple(self, mock_openai_client):
        """Test adding items to the conversation."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        await session.add_items(items)

        mock_openai_client.conversations.items.create.assert_called_once_with(
            conversation_id="test_id", items=items
        )

    @pytest.mark.asyncio
    async def test_add_items_creates_session_id(self, mock_openai_client):
        """Test that add_items creates session_id if it doesn't exist."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        items: list[TResponseInputItem] = [{"role": "user", "content": "Hello"}]

        await session.add_items(items)

        # Should create conversation first
        mock_openai_client.conversations.create.assert_called_once_with(items=[])
        # Then add items
        mock_openai_client.conversations.items.create.assert_called_once_with(
            conversation_id="test_conversation_id", items=items
        )

    @pytest.mark.asyncio
    async def test_add_items_empty_does_not_create_session(self, mock_openai_client):
        """Test that add_items with no items does not create a remote conversation."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        await session.add_items([])

        mock_openai_client.conversations.create.assert_not_called()
        mock_openai_client.conversations.items.create.assert_not_called()
        with pytest.raises(ValueError, match="add_items\\(\\) with a non-empty list"):
            _ = session.session_id

    @pytest.mark.asyncio
    async def test_add_items_empty_keeps_existing_session_id(self, mock_openai_client):
        """Test that add_items with no items leaves an initialized session untouched."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        await session.add_items([])

        mock_openai_client.conversations.create.assert_not_called()
        mock_openai_client.conversations.items.create.assert_not_called()
        assert session.session_id == "test_id"

    @pytest.mark.asyncio
    async def test_pop_item_with_items(self, mock_openai_client):
        """Test popping item when items exist using method patching."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        # Mock the already-locked read to return one item
        latest_item = {"id": "item_123", "role": "assistant", "content": "Latest message"}

        with patch.object(session, "_get_items", return_value=[latest_item]):
            popped_item = await session.pop_item()

            assert popped_item == latest_item
            mock_openai_client.conversations.items.delete.assert_called_once_with(
                conversation_id="test_id", item_id="item_123"
            )

    @pytest.mark.asyncio
    async def test_pop_item_empty_session(self, mock_openai_client):
        """Test popping item from empty session."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        # Mock the already-locked read to return empty list
        with patch.object(session, "_get_items", return_value=[]):
            popped_item = await session.pop_item()

            assert popped_item is None
            mock_openai_client.conversations.items.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_session(self, mock_openai_client):
        """Test clearing the entire session."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        await session.clear_session()

        # Should delete the conversation and clear session ID
        mock_openai_client.conversations.delete.assert_called_once_with(conversation_id="test_id")
        assert session._session_id is None

    @pytest.mark.asyncio
    async def test_clear_session_cancellation_settles_delete_before_reinitializing(
        self, mock_openai_client
    ):
        """A cancelled clear must settle deletion before the session can be reused."""
        delete_started = asyncio.Event()
        allow_delete_finish = asyncio.Event()
        delete_finished = False

        async def slow_delete(*, conversation_id: str) -> None:
            nonlocal delete_finished
            assert conversation_id == "old_id"
            delete_started.set()
            await allow_delete_finish.wait()
            delete_finished = True

        mock_openai_client.conversations.delete.side_effect = slow_delete
        session = OpenAIConversationsSession(
            conversation_id="old_id", openai_client=mock_openai_client
        )
        clear_task = asyncio.create_task(session.clear_session())

        try:
            await delete_started.wait()
            clear_task.cancel("caller-cancelled")
            await asyncio.sleep(0)

            allow_delete_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await clear_task

            items: list[Any] = [{"role": "user", "content": "Next turn"}]
            await session.add_items(items)
        finally:
            allow_delete_finish.set()
            if not clear_task.done():
                clear_task.cancel()
                await asyncio.gather(clear_task, return_exceptions=True)

        assert session.session_id == "test_conversation_id"
        assert delete_finished is True
        mock_openai_client.conversations.delete.assert_awaited_once_with(conversation_id="old_id")
        mock_openai_client.conversations.create.assert_awaited_once_with(items=[])
        mock_openai_client.conversations.items.create.assert_awaited_once_with(
            conversation_id="test_conversation_id", items=items
        )

    @pytest.mark.asyncio
    async def test_clear_session_cancellation_preserves_replacement_session_id(
        self, mock_openai_client
    ):
        """A settled delete must not clear a replacement conversation ID."""
        delete_started = asyncio.Event()
        allow_delete_finish = asyncio.Event()

        async def slow_delete(*, conversation_id: str) -> None:
            assert conversation_id == "old_id"
            delete_started.set()
            await allow_delete_finish.wait()

        mock_openai_client.conversations.delete.side_effect = slow_delete
        session = OpenAIConversationsSession(
            conversation_id="old_id", openai_client=mock_openai_client
        )
        clear_task = asyncio.create_task(session.clear_session())

        try:
            await delete_started.wait()
            clear_task.cancel("caller-cancelled")
            await asyncio.sleep(0)

            session.session_id = "replacement_id"
            allow_delete_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await clear_task

            items: list[Any] = [{"role": "user", "content": "Next turn"}]
            await session.add_items(items)
        finally:
            allow_delete_finish.set()
            if not clear_task.done():
                clear_task.cancel()
                await asyncio.gather(clear_task, return_exceptions=True)

        assert session.session_id == "replacement_id"
        mock_openai_client.conversations.delete.assert_awaited_once_with(conversation_id="old_id")
        mock_openai_client.conversations.create.assert_not_awaited()
        mock_openai_client.conversations.items.create.assert_awaited_once_with(
            conversation_id="replacement_id", items=items
        )

    @pytest.mark.asyncio
    async def test_clear_session_uninitialized_does_not_create_session(self, mock_openai_client):
        """Test that clear_session on an uninitialized session does not call create or delete."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        await session.clear_session()

        mock_openai_client.conversations.create.assert_not_called()
        mock_openai_client.conversations.delete.assert_not_called()
        assert session._session_id is None

    @pytest.mark.asyncio
    async def test_clear_session_uninitialized_no_api_calls_on_create_failure(
        self, mock_openai_client
    ):
        """Test that clear_session on an uninitialized session succeeds even if create raises."""
        mock_openai_client.conversations.create.side_effect = RuntimeError("API connection error")
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        await session.clear_session()

        mock_openai_client.conversations.create.assert_not_called()
        mock_openai_client.conversations.delete.assert_not_called()
        assert session._session_id is None

    @pytest.mark.asyncio
    async def test_clear_session_failed_delete_retains_session_id(self, mock_openai_client):
        """Test that a failed delete retains the session ID for potential retries."""
        mock_openai_client.conversations.delete.side_effect = RuntimeError("Delete failed")
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        with pytest.raises(RuntimeError, match="Delete failed"):
            await session.clear_session()

        assert session._session_id == "test_id"

    @pytest.mark.asyncio
    async def test_clear_session_retry_after_failed_delete(self, mock_openai_client):
        """Test that retrying clear_session after a failed delete targets the same ID
        without calling create.
        """
        mock_openai_client.conversations.delete.side_effect = [
            RuntimeError("Transient delete error"),
            None,
        ]
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        with pytest.raises(RuntimeError, match="Transient delete error"):
            await session.clear_session()

        assert session._session_id == "test_id"

        # Retry clear_session
        await session.clear_session()

        mock_openai_client.conversations.create.assert_not_called()
        assert mock_openai_client.conversations.delete.call_count == 2
        mock_openai_client.conversations.delete.assert_called_with(conversation_id="test_id")
        assert session._session_id is None

    @pytest.mark.asyncio
    async def test_clear_session_concurrent_get_does_not_clobber_new_session_id(
        self, mock_openai_client
    ):
        """Test that a concurrent _get_session_id during clear_session waits for lock
        and preserves new ID.
        """

        session = OpenAIConversationsSession(
            conversation_id="old_id", openai_client=mock_openai_client
        )
        mock_openai_client.conversations.create.return_value = MagicMock(id="new_id")

        delete_started = asyncio.Event()
        allow_delete_finish = asyncio.Event()

        async def slow_delete(*args: Any, **kwargs: Any) -> Any:
            delete_started.set()
            await allow_delete_finish.wait()
            return None

        mock_openai_client.conversations.delete.side_effect = slow_delete

        clear_task = asyncio.create_task(session.clear_session())
        await delete_started.wait()

        # Concurrently attempt _get_session_id() while clear_session is deleting
        get_task = asyncio.create_task(session._get_session_id())

        # Allow delete to complete
        allow_delete_finish.set()
        await clear_task
        new_id = await get_task

        assert new_id == "new_id"
        assert session._session_id == "new_id"
        mock_openai_client.conversations.create.assert_called_once_with(items=[])


class TestOpenAIConversationsSessionBatches:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mutation", ["add", "pop", "clear", "read"])
    async def test_operations_wait_for_complete_append(self, mock_openai_client, mutation: str):
        stored: list[TResponseInputItem] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        other_started = asyncio.Event()
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"role": "user", "content": f"message {i}", "id": f"msg_{i}"})
            for i in range(21)
        ]
        other: TResponseInputItem = {"role": "user", "content": "other"}

        async def create(*, conversation_id, items):
            stored.extend(items)
            if len(stored) == 20:
                first_started.set()
                await release_first.wait()

        async def delete_item(*, conversation_id, item_id):
            assert stored[-1]["id"] == item_id
            stored.pop()

        async def delete_conversation(*, conversation_id):
            stored.clear()

        mock_openai_client.conversations.items.create.side_effect = create
        mock_openai_client.conversations.items.delete.side_effect = delete_item
        mock_openai_client.conversations.delete.side_effect = delete_conversation
        session = OpenAIConversationsSession(
            conversation_id="conv_test", openai_client=mock_openai_client
        )

        async def list_items(*, conversation_id, order):
            snapshot = stored[:] if order == "asc" else list(reversed(stored))
            for item in snapshot:
                yield MagicMock(model_dump=MagicMock(return_value=item))

        mock_openai_client.conversations.items.list = MagicMock(side_effect=list_items)

        async def mutate():
            other_started.set()
            if mutation == "add":
                await session.add_items([other])
            elif mutation == "pop":
                return await session.pop_item()
            elif mutation == "read":
                return await session.get_items()
            else:
                await session.clear_session()
            return None

        append = asyncio.create_task(session.add_items(items))
        follower = None
        try:
            await asyncio.wait_for(first_started.wait(), timeout=5)
            follower = asyncio.create_task(mutate())
            await asyncio.wait_for(other_started.wait(), timeout=5)
            assert not follower.done()
            assert stored == items[:20]
            release_first.set()
            await append
            result = await asyncio.wait_for(follower, timeout=5)
        finally:
            release_first.set()
            tasks = [append] + ([follower] if follower is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if mutation == "add":
            assert stored == items + [other]
        elif mutation == "read":
            assert result == items
            assert stored == items
        elif mutation == "pop":
            assert result == items[-1]
            assert stored == items[:-1]
        else:
            assert stored == []
            assert session._session_id is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("count", "expected_sizes"), [(0, []), (20, [20]), (21, [20, 1]), (41, [20, 20, 1])]
    )
    async def test_request_sizes_and_order(self, count: int, expected_sizes: list[int]):
        batches: list[list[dict[str, Any]]] = []

        def capture(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/conversations/conv_test/items"
            batches.append(json.loads(request.content)["items"])
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"message {i}"} for i in range(count)
        ]
        async with AsyncOpenAI(
            api_key="test-placeholder",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = OpenAIConversationsSession(conversation_id="conv_test", openai_client=client)
            await session.add_items(items)

        assert [len(batch) for batch in batches] == expected_sizes
        assert [item for batch in batches for item in batch] == items

    @pytest.mark.asyncio
    async def test_later_failure_preserves_prefix_and_stops(self):
        batches: list[list[dict[str, Any]]] = []
        saved: list[dict[str, Any]] = []

        def capture(request: httpx.Request) -> httpx.Response:
            batch = json.loads(request.content)["items"]
            batches.append(batch)
            if len(batches) == 2:
                return httpx.Response(400, json={"error": {"message": "synthetic failure"}})
            saved.extend(batch)
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"message {i}"} for i in range(41)
        ]
        async with AsyncOpenAI(
            api_key="test-placeholder",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = OpenAIConversationsSession(conversation_id="conv_test", openai_client=client)
            with pytest.raises(BadRequestError, match="synthetic failure") as caught:
                await session.add_items(items)
            assert caught.value.status_code == 400
            await session.add_items(items[40:])

        assert batches == [items[:20], items[20:40], items[40:]]
        assert saved == items[:20] + items[40:]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("request_fails", [False, True])
    async def test_cancelled_batch_settles_before_queued_writer(self, request_fails: bool):
        batches: list[list[dict[str, Any]]] = []
        saved: list[dict[str, Any]] = []
        second_started = asyncio.Event()
        release_request = asyncio.Event()
        follower_started = asyncio.Event()
        remote_tasks: list[asyncio.Task[None]] = []

        async def remote_commit(batch: list[dict[str, Any]]) -> None:
            await release_request.wait()
            if not request_fails:
                saved.extend(batch)

        async def capture(request: httpx.Request) -> httpx.Response:
            batch = json.loads(request.content)["items"]
            batches.append(batch)
            if len(batches) == 2:
                # An accepted server mutation survives cancellation of the HTTP caller.
                remote = asyncio.create_task(remote_commit(batch))
                remote_tasks.append(remote)
                second_started.set()
                await asyncio.shield(remote)
                if request_fails:
                    return httpx.Response(400, json={"error": {"message": "synthetic failure"}})
            else:
                saved.extend(batch)
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"message {i}"} for i in range(41)
        ]
        survivor: list[TResponseInputItem] = [{"role": "user", "content": "queued writer"}]
        async with AsyncOpenAI(
            api_key="test-placeholder",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = OpenAIConversationsSession(conversation_id="conv_test", openai_client=client)

            async def append_survivor() -> None:
                follower_started.set()
                await session.add_items(survivor)

            cancellations: list[asyncio.CancelledError] = []

            async def append_cancelled() -> None:
                try:
                    await session.add_items(items)
                except asyncio.CancelledError as exc:
                    # Python 3.10 can drop the message when a task exposes cancellation.
                    cancellations.append(exc)
                    raise

            write = asyncio.create_task(append_cancelled())
            follower = None
            try:
                await asyncio.wait_for(second_started.wait(), timeout=5)
                follower = asyncio.create_task(append_survivor())
                await asyncio.wait_for(follower_started.wait(), timeout=5)
                write.cancel("original cancellation")
                await asyncio.sleep(0)
                write.cancel("repeated cancellation")
                await asyncio.sleep(0)
                assert not write.done()
                assert not follower.done()
                assert saved == items[:20]
                assert batches == [items[:20], items[20:40]]
                release_request.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(write, timeout=5)
                assert len(cancellations) == 1
                assert cancellations[0].args == ("original cancellation",)
                await asyncio.wait_for(follower, timeout=5)
            finally:
                release_request.set()
                await asyncio.gather(
                    write,
                    *([follower] if follower is not None else []),
                    *remote_tasks,
                    return_exceptions=True,
                )

        assert batches == [items[:20], items[20:40], survivor]
        assert saved == (items[:20] if request_fails else items[:40]) + survivor

    @pytest.mark.asyncio
    async def test_single_request_cancellation_does_not_wait_for_response(self):
        started = asyncio.Event()
        release_request = asyncio.Event()

        async def capture(request: httpx.Request) -> httpx.Response:
            started.set()
            await release_request.wait()
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        async with AsyncOpenAI(
            api_key="test-placeholder",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = OpenAIConversationsSession(conversation_id="conv_test", openai_client=client)
            write = asyncio.create_task(
                session.add_items([{"role": "user", "content": "message"}] * 20)
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                write.cancel()
                done, _ = await asyncio.wait({write}, timeout=5)
                assert write in done
                assert write.cancelled()
            finally:
                release_request.set()
                await asyncio.gather(write, return_exceptions=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_runner_batches_input_with_lazy_creation(self, stream: bool):
        requests: list[tuple[str, str]] = []
        batches: list[list[dict[str, Any]]] = []

        def capture(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/v1/conversations":
                assert json.loads(request.content) == {"items": []}
                return httpx.Response(
                    200, json={"id": "conv_test", "object": "conversation", "created_at": 0}
                )
            assert request.url.path == "/v1/conversations/conv_test/items"
            if request.method == "POST":
                batches.append(json.loads(request.content)["items"])
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"message {i}"} for i in range(41)
        ]
        model = ScriptedModel()
        model.enqueue([get_text_message("done")])
        agent = Agent(name="test", model=model)
        async with AsyncOpenAI(
            api_key="test-placeholder",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = OpenAIConversationsSession(openai_client=client)
            if stream:
                result = Runner.run_streamed(
                    agent, items, session=session, run_config=RunConfig(tracing_disabled=True)
                )
                async for _ in result.stream_events():
                    pass
                assert result.final_output == "done"
            else:
                result_sync = await Runner.run(
                    agent, items, session=session, run_config=RunConfig(tracing_disabled=True)
                )
                assert result_sync.final_output == "done"

        assert requests.count(("POST", "/v1/conversations")) == 1
        assert [len(batch) for batch in batches] == [20, 20, 1, 1]
        assert [item for batch in batches[:3] for item in batch] == items
        assert batches[3][0]["content"][0]["text"] == "done"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize(
        "history_size,failure",
        [(1, "partial"), (40, "partial"), (1, "before"), (1, "lost-ack")],
    )
    async def test_runner_resume_does_not_replay_partial_repeated_history(
        self, stream: bool, history_size: int, failure: str
    ):
        """A repeated boundary must not disguise a partial append as unchanged history."""
        messages = [
            get_text_message(f"message {i}").model_copy(update={"id": f"msg_{i}"})
            for i in range(19)
        ] + [get_text_message("S").model_copy(update={"id": "msg_19"})]
        # A long periodic history exercises the bounded saved tail as well as the
        # original one-item counterexample. Provider IDs differ at every occurrence.
        stored: list[dict[str, Any]] = [
            {**messages[i % 20].model_dump(exclude_none=True), "id": f"old_{i}"}
            for i in range(history_size - 1)
        ]
        writes: list[list[dict[str, Any]]] = []
        effects: list[str] = []

        def capture(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                data = stored[::-1] if request.url.params.get("order") == "desc" else stored[:]
                return httpx.Response(200, json={"object": "list", "data": data, "has_more": False})
            batch = json.loads(request.content)["items"]
            writes.append(batch)
            fail = len(writes) == (2 if failure == "before" else 3)
            if not fail or failure == "lost-ack":
                for item in batch:
                    stored.append({**item, "id": f"item_{len(stored)}"})
            if fail:
                return httpx.Response(
                    400,
                    json={
                        "error": {"message": "synthetic failure", "type": "invalid_request_error"}
                    },
                )
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        @tool(needs_approval=True)
        async def lookup() -> str:
            effects.append("lookup")
            return "found"

        @tool
        async def finish() -> str:
            return "done"

        @output_guardrail
        async def gate(ctx, agent, output):
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

        model = ScriptedModel(
            [
                messages + [get_function_tool_call("lookup", "{}", call_id="lookup_1")],
                [get_function_tool_call("finish", "{}", call_id="finish_1")],
            ]
        )
        agent = Agent(
            name="test",
            model=model,
            tools=[lookup, finish],
            output_guardrails=[gate],
            tool_use_behavior={"stop_at_tool_names": ["finish"]},
        )
        config = RunConfig(tracing_disabled=True)
        async with AsyncOpenAI(
            api_key="test-placeholder",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = OpenAIConversationsSession(conversation_id="conv_test", openai_client=client)

            async def run(value):
                if stream:
                    result = Runner.run_streamed(agent, value, session=session, run_config=config)
                    async for _ in result.stream_events():
                        pass
                    return result
                return await Runner.run(agent, value, session=session, run_config=config)

            original = get_text_message("S").model_dump(exclude_none=True)
            original.pop("id")
            paused = await run([cast(TResponseInputItem, original)])
            assert len(stored) == history_size
            state = paused.to_state()
            state.approve(state.get_interruptions()[0])
            if stream:
                failed = Runner.run_streamed(agent, state, session=session, run_config=config)
                with pytest.raises(BadRequestError, match="synthetic failure"):
                    async for _ in failed.stream_events():
                        pass
                state = failed.to_state()
            else:
                with pytest.raises(BadRequestError, match="synthetic failure"):
                    await run(state)
            assert effects == ["lookup"]
            assert len(model.calls) == 1
            state = await RunState.from_json(agent, json.loads(json.dumps(state.to_json())))
            if failure == "partial":
                assert len(stored) == history_size + 20
                snapshot = stored[:]
                with pytest.raises(UserError, match="Cannot reconcile the pending Session write"):
                    await run(state)
                assert stored == snapshot
                assert [len(batch) for batch in writes] == [1, 20, 2]
                assert len(model.calls) == 1
            else:
                result = await run(state)
                assert result.final_output == "done"
                texts = [item["content"][0]["text"] for item in stored if item["type"] == "message"]
                assert texts == ["S"] + [f"message {i}" for i in range(19)] + ["S"]
                assert len(model.calls) == 2
            assert effects == ["lookup"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "atomic,output_count,history_size",
        [(False, 23, 30), (True, 23, 30), (False, 2, 30), (False, 23, 1), (False, 21, 1)],
    )
    async def test_runner_resume_handles_periodic_history_for_session_atomicity(
        self, atomic: bool, output_count: int, history_size: int
    ):
        """A bounded periodic tail cannot prove every item in a failed append was saved."""
        messages = [
            get_text_message("ABC"[i % 3]).model_copy(update={"id": f"msg_{i}"}) for i in range(30)
        ]
        stored: list[dict[str, Any]] = [
            {**item.model_dump(exclude_none=True), "id": f"old_{i}"}
            for i, item in enumerate(messages[: history_size - 1])
        ]
        writes: list[list[dict[str, Any]]] = []

        def capture(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                data = stored[::-1] if request.url.params.get("order") == "desc" else stored[:]
                return httpx.Response(200, json={"object": "list", "data": data, "has_more": False})
            batch = json.loads(request.content)["items"]
            writes.append(batch)
            fail = len(writes) == (2 if output_count <= 20 else 3)
            if not fail or output_count <= 20 or history_size == 1:
                for item in batch:
                    stored.append({**item, "id": f"item_{len(stored)}"})
            if fail:
                return httpx.Response(
                    400,
                    json={
                        "error": {"message": "synthetic failure", "type": "invalid_request_error"}
                    },
                )
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

        class LostAckSession(IdStrippingSession):
            async def add_items(self, items: list[TResponseInputItem]) -> None:
                writes.append(cast(list[dict[str, Any]], items))
                await super().add_items(items)
                if len(writes) == 2:
                    raise RuntimeError("synthetic failure")

        def retain_messages(data: HandoffInputData) -> HandoffInputData:
            return data.clone(
                new_items=tuple(
                    item for item in data.new_items if isinstance(item, MessageOutputItem)
                )
            )

        model = ScriptedModel(
            [
                messages[:output_count]
                + [get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff_1")],
                [get_text_message("done")],
            ]
        )
        delegate = Agent(name="delegate", model=model)
        agent = Agent(
            name="test", model=model, handoffs=[handoff(delegate, input_filter=retain_messages)]
        )
        config = RunConfig(tracing_disabled=True)
        async with AsyncOpenAI(
            api_key="test-placeholder",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        ) as client:
            session = (
                LostAckSession(history=cast(list[TResponseInputItem], stored))
                if atomic
                else OpenAIConversationsSession(conversation_id="conv_test", openai_client=client)
            )
            original = messages[29].model_dump(exclude_none=True)
            original.pop("id")
            failed = Runner.run_streamed(
                agent, [cast(TResponseInputItem, original)], session=session, run_config=config
            )
            with pytest.raises(
                RuntimeError if atomic else BadRequestError, match="synthetic failure"
            ):
                async for _ in failed.stream_events():
                    pass
            snapshot = await session.get_items()
            assert len(snapshot) == (
                history_size + output_count
                if atomic or output_count <= 20 or history_size == 1
                else history_size + 20
            )
            assert [len(batch) for batch in writes] == (
                [1, output_count] if atomic or output_count <= 20 else [1, 20, output_count - 20]
            )
            state = await RunState.from_json(agent, failed.to_state().to_json())
            if atomic or output_count <= 20 or history_size == 1:
                resumed = await Runner.run(agent, state, session=session, run_config=config)
                assert resumed.final_output == "done"
                history = await session.get_items()
                assert history[:-1] == snapshot
                assert len(history) == history_size + output_count + 1
                assert [len(batch) for batch in writes] == (
                    [1, output_count, 1]
                    if atomic or output_count <= 20
                    else [1, 20, output_count - 20, 1]
                )
                assert len(model.calls) == 2
            else:
                with pytest.raises(UserError, match="Cannot reconcile the pending Session write"):
                    await Runner.run(agent, state, session=session, run_config=config)
                assert await session.get_items() == snapshot
                assert len(writes) == 3
                assert len(model.calls) == 1
                # Missing messages remain pending rather than being discarded.
                assert len(state.to_json()["pending_session_write"]["items"]) == 23


class TestOpenAIConversationsSessionRunnerIntegration:
    """Test integration with Agent Runner using simple mocking."""

    @pytest.mark.asyncio
    async def test_runner_integration_basic(self, agent: Agent, mock_openai_client):
        """Test that OpenAIConversationsSession works with Agent Runner."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Mock the session methods to avoid complex async iterator setup
        with patch.object(session, "get_items", return_value=[]):
            with patch.object(session, "add_items") as mock_add_items:
                # Run the agent
                assert isinstance(agent.model, ScriptedModel)
                agent.model.enqueue([get_text_message("San Francisco")])

                result = await Runner.run(
                    agent, "What city is the Golden Gate Bridge in?", session=session
                )

                assert result.final_output == "San Francisco"

                # Verify session interactions occurred
                mock_add_items.assert_called()

    @pytest.mark.asyncio
    async def test_runner_with_conversation_history(self, agent: Agent, mock_openai_client):
        """Test that conversation history is preserved across Runner calls."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Mock conversation history
        conversation_history = [
            {"role": "user", "content": "What city is the Golden Gate Bridge in?"},
            {"role": "assistant", "content": "San Francisco"},
        ]

        with patch.object(session, "get_items", return_value=conversation_history):
            with patch.object(session, "add_items"):
                # Second turn - should have access to previous conversation
                assert isinstance(agent.model, ScriptedModel)
                agent.model.enqueue([get_text_message("California")])

                result = await Runner.run(agent, "What state is it in?", session=session)

                assert result.final_output == "California"

                # Verify that the model received the conversation history
                last_input = agent.model.calls[-1].input
                assert len(last_input) > 1  # Should include previous messages

                # Check that previous conversation is included
                input_contents = [str(item.get("content", "")) for item in last_input]
                assert any("Golden Gate Bridge" in content for content in input_contents)

    @pytest.mark.asyncio
    async def test_runner_persists_program_item_ids(self, mock_openai_client):
        """Program items keep the id the Conversations create-item schema requires."""
        model = ScriptedModel()
        model.extend(
            [
                [
                    Program(
                        id="program_item",
                        call_id="call_program",
                        code='lookup_inventory(sku="A-1")',
                        fingerprint="fingerprint",
                        type="program",
                    ),
                ],
                [
                    ProgramOutput(
                        id="program_output_item",
                        call_id="call_program",
                        result='{"sku":"A-1","available_units":42}',
                        status="completed",
                        type="program_output",
                    ),
                    get_text_message("done"),
                ],
            ]
        )

        @function_tool(allowed_callers=["programmatic"])
        def lookup_inventory(sku: str) -> str:
            return sku

        program_agent = Agent(
            name="inventory",
            model=model,
            tools=[ProgrammaticToolCallingTool(), lookup_inventory],
        )
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        saved: list[TResponseInputItem] = []

        async def record(items: list[TResponseInputItem]) -> None:
            saved.extend(items)

        with patch.object(session, "get_items", return_value=[]):
            with patch.object(session, "add_items", side_effect=record):
                result = await Runner.run(program_agent, "Check inventory", session=session)

        assert result.final_output == "done"

        saved_items = {
            item["type"]: item
            for item in cast(list[dict[str, Any]], saved)
            if isinstance(item, dict) and "type" in item
        }
        assert saved_items["program"]["id"] == "program_item"
        assert saved_items["program_output"]["id"] == "program_output_item"
        # Item types whose id the Conversations schema leaves optional stay stripped.
        assert "id" not in saved_items["message"]


class TestOpenAIConversationsSessionErrorHandling:
    """Test error handling for various failure scenarios."""

    @pytest.mark.asyncio
    async def test_api_failure_during_conversation_creation(self, mock_openai_client):
        """Test handling of API failures during conversation creation."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Mock API failure
        mock_openai_client.conversations.create.side_effect = Exception("API Error")

        with pytest.raises(Exception, match="API Error"):
            await session._get_session_id()

        mock_openai_client.conversations.create.side_effect = None
        mock_openai_client.conversations.create.return_value = MagicMock(id="retry_id")

        assert await session._get_session_id() == "retry_id"
        assert mock_openai_client.conversations.create.call_count == 2

    @pytest.mark.asyncio
    async def test_api_failure_during_add_items(self, mock_openai_client):
        """Test handling of API failures during add_items."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        mock_openai_client.conversations.items.create.side_effect = Exception("Add items failed")

        items: list[TResponseInputItem] = [{"role": "user", "content": "Hello"}]

        with pytest.raises(Exception, match="Add items failed"):
            await session.add_items(items)

    @pytest.mark.asyncio
    async def test_api_failure_during_clear_session(self, mock_openai_client):
        """Test handling of API failures during clear_session."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        mock_openai_client.conversations.delete.side_effect = Exception("Clear session failed")

        with pytest.raises(Exception, match="Clear session failed"):
            await session.clear_session()

    @pytest.mark.asyncio
    async def test_invalid_item_id_in_pop_item(self, mock_openai_client):
        """Test handling of invalid item ID during pop_item."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        # Mock item without ID
        invalid_item = {"role": "assistant", "content": "No ID"}

        with patch.object(session, "_get_items", return_value=[invalid_item]):
            # This should raise a KeyError because 'id' field is missing
            with pytest.raises(KeyError, match="'id'"):
                await session.pop_item()


class TestOpenAIConversationsSessionConcurrentAccess:
    """Test concurrent access patterns with simple scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_sessions_different_conversation_ids(self, mock_openai_client):
        """Test that multiple sessions with different conversation IDs are isolated."""
        session1 = OpenAIConversationsSession(
            conversation_id="conversation_1", openai_client=mock_openai_client
        )
        session2 = OpenAIConversationsSession(
            conversation_id="conversation_2", openai_client=mock_openai_client
        )

        items1: list[TResponseInputItem] = [{"role": "user", "content": "Session 1 message"}]
        items2: list[TResponseInputItem] = [{"role": "user", "content": "Session 2 message"}]

        # Add items to both sessions
        await session1.add_items(items1)
        await session2.add_items(items2)

        # Verify calls were made with correct conversation IDs
        assert mock_openai_client.conversations.items.create.call_count == 2

        # Check the calls
        calls = mock_openai_client.conversations.items.create.call_args_list
        assert calls[0][1]["conversation_id"] == "conversation_1"
        assert calls[0][1]["items"] == items1
        assert calls[1][1]["conversation_id"] == "conversation_2"
        assert calls[1][1]["items"] == items2

    @pytest.mark.asyncio
    async def test_session_id_lazy_creation_consistency(self, mock_openai_client):
        """Test that session ID creation is consistent across multiple calls."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Call _get_session_id multiple times
        id1 = await session._get_session_id()
        id2 = await session._get_session_id()
        id3 = await session._get_session_id()

        # All should return the same session ID
        assert id1 == id2 == id3 == "test_conversation_id"

        # Conversation should only be created once
        mock_openai_client.conversations.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_first_writes_share_one_conversation(self, mock_openai_client):
        """Test that concurrent first writes cannot split session history."""
        create_started = asyncio.Event()
        release_create = asyncio.Event()
        creation_count = 0

        async def create_conversation(*, items):
            nonlocal creation_count
            creation_count += 1
            conversation_id = f"conversation_{creation_count}"
            create_started.set()
            await release_create.wait()
            return MagicMock(id=conversation_id)

        mock_openai_client.conversations.create.side_effect = create_conversation
        session = OpenAIConversationsSession(openai_client=mock_openai_client)
        first_items: list[TResponseInputItem] = [{"role": "user", "content": "First message"}]
        second_items: list[TResponseInputItem] = [{"role": "user", "content": "Second message"}]

        first_write = asyncio.create_task(session.add_items(first_items))
        await create_started.wait()
        second_write = asyncio.create_task(session.add_items(second_items))
        await asyncio.sleep(0)
        release_create.set()
        await asyncio.gather(first_write, second_write)

        mock_openai_client.conversations.create.assert_called_once_with(items=[])
        writes = mock_openai_client.conversations.items.create.call_args_list
        assert len(writes) == 2
        assert {call.kwargs["conversation_id"] for call in writes} == {session.session_id}

    @pytest.mark.asyncio
    async def test_concurrent_first_write_recovers_after_creation_failure(self, mock_openai_client):
        """Test that a waiting writer recovers when the first initializer fails."""
        first_create_started = asyncio.Event()
        release_first_create = asyncio.Event()
        creation_count = 0

        async def create_conversation(*, items):
            nonlocal creation_count
            creation_count += 1
            if creation_count == 1:
                first_create_started.set()
                await release_first_create.wait()
                raise RuntimeError("Conversation creation failed")
            return MagicMock(id="surviving_conversation")

        mock_openai_client.conversations.create.side_effect = create_conversation
        session = OpenAIConversationsSession(openai_client=mock_openai_client)
        failed_items: list[TResponseInputItem] = [{"role": "user", "content": "Failed writer"}]
        surviving_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Surviving writer"}
        ]

        failed_write = asyncio.create_task(session.add_items(failed_items))
        await first_create_started.wait()
        surviving_write = asyncio.create_task(session.add_items(surviving_items))
        await asyncio.sleep(0)

        mock_openai_client.conversations.create.assert_called_once_with(items=[])
        release_first_create.set()

        with pytest.raises(RuntimeError, match="Conversation creation failed"):
            await failed_write
        await surviving_write

        assert mock_openai_client.conversations.create.call_count == 2
        mock_openai_client.conversations.items.create.assert_called_once_with(
            conversation_id="surviving_conversation", items=surviving_items
        )
        assert session.session_id == "surviving_conversation"


# ============================================================================
# SessionSettings Tests
# ============================================================================


class TestOpenAIConversationsSessionSettings:
    """Test SessionSettings integration with OpenAIConversationsSession."""

    def test_session_settings_default(self, mock_openai_client):
        """Test that session_settings defaults to empty SessionSettings."""
        from agents.memory import SessionSettings

        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Should have default SessionSettings
        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit is None

    def test_session_settings_constructor(self, mock_openai_client):
        """Test passing session_settings via constructor."""
        from agents.memory import SessionSettings

        session = OpenAIConversationsSession(
            openai_client=mock_openai_client, session_settings=SessionSettings(limit=5)
        )

        assert session.session_settings is not None
        assert session.session_settings.limit == 5

    def test_session_settings_constructor_normalizes_dictionary(self, mock_openai_client):
        from agents.memory import SessionSettings

        session = OpenAIConversationsSession(
            openai_client=mock_openai_client,
            session_settings={"limit": 0},
        )

        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit == 0
