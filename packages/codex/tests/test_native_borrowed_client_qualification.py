"""Borrowed client credentials and named producers run through native Sessions."""

import asyncio
import json

import httpx
from openai import AsyncOpenAI

from agents import Agent, ChatCompletionsModel, ModelRequest, ModelResponse, NativeProvider
from tests._asyncio import timeout


async def test_borrowed_chat_client_refreshes_async_credentials_on_each_native_request():
    keys = iter(["synthetic-first-key", "synthetic-second-key"])
    authorizations: list[str] = []
    requests: list[dict] = []

    async def api_key() -> str:
        await asyncio.sleep(0)
        return next(keys)

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://borrowed-chat.invalid/v1/chat/completions"
        authorizations.append(request.headers["authorization"])
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": f"chat-{len(requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": "borrowed-chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"reply-{len(requests)}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://borrowed-chat.invalid/v1",
        http_client=transport,
        max_retries=0,
    )
    try:
        async with (
            timeout(20),
            NativeProvider(
                api_key="synthetic-native-owner",
                model_backend=ChatCompletionsModel(client, "borrowed-chat-model"),
            ) as provider,
        ):
            async with Agent(
                instructions="Answer briefly.", provider=provider
            ).session() as session:
                first = await session.submit("first native request")
                assert (await first.get_result()).final_output == "reply-1"
                second = await session.submit("second native request")
                assert (await second.get_result()).final_output == "reply-2"
        assert authorizations == ["Bearer synthetic-first-key", "Bearer synthetic-second-key"]
        assert len(requests) == 2
        assert all(request["model"] == "borrowed-chat-model" for request in requests)
        assert any(
            item["role"] == "assistant" and item["content"] == [{"type": "text", "text": "reply-1"}]
            for item in requests[1]["messages"]
        )
        assert not client.is_closed()
        assert not transport.is_closed
    finally:
        await client.close()


async def test_native_provider_resolves_namespaced_models_to_distinct_producers():
    selected: list[str] = []
    completed: list[tuple[str, str]] = []

    class Model:
        def __init__(self, name: str):
            self.name = name

        def complete(self, request: ModelRequest) -> ModelResponse:
            completed.append((self.name, request.body["model"]))
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": self.name}],
                    }
                ]
            )

    models = {name: Model(name) for name in ("first/model-a", "second/model-b")}

    class Factory:
        async def get_model(self, name: str) -> Model:
            await asyncio.sleep(0)
            selected.append(name)
            return models[name]

    async with (
        timeout(20),
        NativeProvider(api_key="synthetic-native-owner", model_backend=Factory()) as provider,
    ):
        for name in models:
            async with Agent(
                model=name, instructions="Answer.", provider=provider
            ).session() as session:
                turn = await session.submit("Which model?")
                assert (await turn.get_result()).final_output == name
    assert selected == list(models)
    assert completed == [(name, name) for name in models]
