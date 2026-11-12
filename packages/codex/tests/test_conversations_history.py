"""Conversations HTTP ownership through the real OpenAI client."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from agents import ConversationsHistory, ConversationsWriteError


def message(index):
    return {
        "id": f"msg_{index}",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": str(index)}],
    }


class Endpoint:
    def __init__(self, items=()):
        self.items = list(items)
        self.requests = []
        self.creates = 0
        self.batches = []
        self.fail_batch = None
        self.entered = asyncio.Event()
        self.release = None

    async def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url, body))
        path = request.url.path
        if path == "/v1/conversations" and request.method == "POST":
            self.creates += 1
            return httpx.Response(
                200, json={"id": f"conv_{self.creates}", "object": "conversation", "created_at": 1}
            )
        if path.endswith("/items") and request.method == "GET":
            values = list(self.items)
            if request.url.params.get("order") == "desc":
                values.reverse()
            after = request.url.params.get("after")
            if after is not None:
                values = values[
                    next(i for i, item in enumerate(values) if item["id"] == after) + 1 :
                ]
            page = values[:2]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": page,
                    "first_id": page[0]["id"] if page else None,
                    "last_id": page[-1]["id"] if page else None,
                    "has_more": len(values) > len(page),
                },
            )
        if path.endswith("/items") and request.method == "POST":
            self.batches.append(body["items"])
            if len(self.batches) == self.fail_batch:
                return httpx.Response(503, json={"error": {"message": "synthetic unavailable"}})
            if self.release is not None:
                self.entered.set()
                await self.release.wait()
            self.items.extend(body["items"])
            return httpx.Response(
                200,
                json={"object": "list", "data": body["items"], "has_more": False},
            )
        if request.method == "DELETE":
            if "/items/" in path:
                item_id = path.rsplit("/", 1)[-1]
                self.items = [item for item in self.items if item["id"] != item_id]
                return httpx.Response(
                    200, json={"id": "conv_existing", "object": "conversation", "created_at": 1}
                )
            self.items.clear()
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[-1],
                    "object": "conversation.deleted",
                    "deleted": True,
                },
            )
        pytest.fail(f"Unexpected synthetic route: {request.method} {path}")


@pytest.fixture
async def endpoint():
    endpoint = Endpoint()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as transport:
        client = AsyncOpenAI(
            api_key="synthetic",
            base_url="https://conversations.invalid/v1",
            http_client=transport,
            max_retries=3,
        )
        yield endpoint, client, transport


async def test_existing_history_paginates_in_order_and_preserves_tool_identity(endpoint):
    peer, client, transport = endpoint
    peer.items = [message(i) for i in range(5)] + [
        {
            "id": "fc_original",
            "type": "function_call",
            "name": "echo",
            "call_id": "exact-call",
            "arguments": "{}",
            "status": "completed",
            "future": {"retained": True},
        },
        {
            "id": "fco_original",
            "type": "function_call_output",
            "call_id": "exact-call",
            "output": "exact receipt",
            "status": "completed",
        },
    ]
    history = ConversationsHistory(client, conversation_id="conv_existing")
    assert await history.get_items() == peer.items
    assert await history.get_items(3) == peer.items[-3:]
    assert peer.creates == 0
    count = len(peer.requests)
    assert await history.get_items(0) == []
    assert len(peer.requests) == count
    assert not transport.is_closed and client.max_retries == 3


async def test_lazy_create_chunked_append_pop_clear_and_recreate(endpoint):
    peer, client, transport = endpoint
    history = ConversationsHistory(client)
    await history.add_items([])
    assert history.conversation_id is None and not peer.requests
    await history.add_items([message(i) for i in range(41)])
    assert [len(batch) for batch in peer.batches] == [20, 20, 1]
    assert history.conversation_id == "conv_1"
    assert await history.pop_item() == message(40)
    assert await history.get_items(1) == [message(39)]
    await history.clear()
    assert history.conversation_id is None and not peer.items
    assert await history.get_conversation_id() == "conv_2"
    assert not transport.is_closed and client.max_retries == 3


async def test_partial_append_retains_acknowledged_prefix_without_automatic_retry(endpoint):
    peer, client, _ = endpoint
    peer.fail_batch = 2
    history = ConversationsHistory(client, conversation_id="conv_existing")
    items = [message(i) for i in range(41)]
    with pytest.raises(ConversationsWriteError) as caught:
        await history.add_items(items)
    assert caught.value.acknowledged == 20
    assert list(caught.value.items) == items
    assert len(peer.batches) == 2
    assert await history.get_items() == items[:20]
    assert client.max_retries == 3


async def test_cancelled_append_joins_current_mutation_before_unlocking(endpoint):
    peer, client, transport = endpoint
    peer.release = asyncio.Event()
    history = ConversationsHistory(client, conversation_id="conv_existing")
    writing = asyncio.create_task(history.add_items([message(i) for i in range(21)]))
    await peer.entered.wait()
    reading = asyncio.create_task(history.get_items())
    writing.cancel()
    await asyncio.sleep(0)
    assert not writing.done() and not reading.done()
    peer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await writing
    assert await reading == [message(i) for i in range(20)]
    assert len(peer.batches) == 1
    assert not transport.is_closed
