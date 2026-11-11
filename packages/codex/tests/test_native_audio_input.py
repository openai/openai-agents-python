"""Native audio admission preserves mixed-content order through tool continuation."""

import json
from typing import Any

import pytest

from agents import Agent, ModelRequest, ModelResponse, NativeProvider, ToolRef, function_tool
from agents.input import InputAudio, InputText, UserMessage
from tests._asyncio import timeout


async def test_native_audio_envelope_rejection_preserves_session() -> None:
    seen: list[dict[str, Any]] = []

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request.body)
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            )

    async with (
        timeout(30),
        NativeProvider(base_url="https://example.invalid/v1", model_backend=Model()) as provider,
        Agent(
            model="custom-model", instructions="Audio admission test", provider=provider
        ).session() as session,
    ):
        overhead = len(json.dumps([{"type": "audio", "audio_url": ""}]))
        with pytest.raises(ValueError, match="Audio input exceeds"):
            turn = await session.submit(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "audio_url": "a" * (64 * 1024 * 1024 - overhead)}
                    ],
                }
            )
            await turn.get_result()
        assert seen == []
        turn = await session.submit("Still usable")
        assert (await turn.get_result()).final_output == "ok"
        assert len(seen) == 1


async def test_native_audio_survives_function_continuation() -> None:
    seen: list[dict[str, Any]] = []
    effects: list[str] = []
    content: list[InputText | InputAudio] = [
        {"type": "input_text", "text": "before"},
        {"type": "input_audio", "audio_url": "data:audio/wav;base64,UklGRg=="},
        {"type": "input_text", "text": "after"},
    ]
    message: UserMessage = {"role": "user", "content": content}

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "found"

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request.body)
            if len(seen) == 1:
                return ModelResponse(
                    [
                        {
                            "type": "function_call",
                            "name": "lookup",
                            "call_id": "call_audio",
                            "arguments": "{}",
                        }
                    ]
                )
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            )

    async with (
        timeout(20),
        NativeProvider(base_url="https://example.invalid/v1", model_backend=Model()) as provider,
    ):
        agent = Agent(
            model="custom-model",
            instructions="Use lookup once.",
            tools=[lookup],
            provider=provider,
            model_settings={
                "tool_choice": ToolRef("lookup"),
                "input_modalities": ["text", "audio"],
            },
        )
        with pytest.raises(ValueError, match="Audio input exceeds"):
            await agent.run(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "audio_url": "https://audio.test/" + "a" * (64 * 1024 * 1024),
                        }
                    ],
                }
            )
        assert seen == []
        assert effects == []
        result = await agent.run(message)
    assert result.final_output == "ok"
    assert effects == ["lookup"]
    assert len(seen) == 2
    for body in seen:
        original = next(item for item in body["input"] if item.get("role") == "user")
        assert original["content"] == content
    assert any(item.get("type") == "function_call_output" for item in seen[1]["input"])


async def test_native_modality_updates_restore_and_provider_isolation() -> None:
    seen: list[dict[str, Any]] = []
    message: UserMessage = {
        "role": "user",
        "content": [{"type": "input_audio", "audio_url": "data:audio/wav;base64,UklGRg=="}],
    }

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request.body)
            assert "input_modalities" not in request.body
            assert "request_controls" not in request.body
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            )

    def has_audio(body: dict[str, Any]) -> bool:
        return any(
            part.get("type") == "input_audio"
            for item in body["input"]
            for part in item.get("content", [])
        )

    async with (
        timeout(40),
        NativeProvider(base_url="https://example.invalid/v1", model_backend=Model()) as provider,
    ):
        declared = Agent(
            model="custom-model",
            instructions="Answer.",
            provider=provider,
            model_settings={"input_modalities": ["text", "audio"]},
        )
        async with declared.session() as session:
            await (await session.submit(message)).get_result()
            assert has_audio(seen[-1])
            audio_state = await session.save_state()
            await session.update(model_settings={"temperature": 0.5})
            await (await session.submit("sparse update preserves audio")).get_result()
            assert has_audio(seen[-1])
            await session.update(model_settings={"input_modalities": ["text"]})
            await (await session.submit(message)).get_result()
            assert not has_audio(seen[-1])
            text_state = await session.save_state()
            await session.update(model_settings={"input_modalities": None})
            await (await session.submit(message)).get_result()
            assert not has_audio(seen[-1])
            cleared_state = await session.save_state()
        for state, expected in [(audio_state, True), (text_state, False), (cleared_state, False)]:
            async with declared.session(state=state) as restored:
                await (await restored.submit(message)).get_result()
                assert has_audio(seen[-1]) is expected
        ordinary = Agent(model="custom-model", instructions="Answer.", provider=provider)
        await ordinary.run(message)
        assert not has_audio(seen[-1])


async def test_native_audio_and_named_selector_through_real_chat_client() -> None:
    import json

    import httpx2 as httpx
    from openai import AsyncOpenAI

    from agents import ChatCompletionsModel

    seen: list[dict[str, Any]] = []
    effects: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        message = (
            {"role": "assistant", "content": "ok"}
            if len(seen) > 1
            else {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_audio_client",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "chat_audio",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop" if len(seen) > 1 else "tool_calls",
                        "message": message,
                    }
                ],
            },
        )

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "found"

    @function_tool
    def other() -> str:
        effects.append("other")
        return "unselected"

    async with (
        timeout(30),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport,
        AsyncOpenAI(api_key="fixture", http_client=transport) as client,
        NativeProvider(
            base_url="https://example.invalid/v1", model_backend=ChatCompletionsModel(client)
        ) as provider,
    ):
        result = await Agent(
            model="custom-model",
            instructions="Use lookup once.",
            tools=[lookup, other],
            provider=provider,
            model_settings={
                "input_modalities": ["text", "audio"],
                "tool_choice": ToolRef("lookup"),
            },
        ).run(
            {
                "role": "user",
                "content": [{"type": "input_audio", "audio_url": "data:audio/wav;base64,UklGRg=="}],
            }
        )
    assert result.final_output == "ok"
    assert effects == ["lookup"]
    assert len(seen) == 2
    assert seen[0]["tool_choice"] == "required"
    assert [item["function"]["name"] for item in seen[0]["tools"]] == ["lookup"]
    for body in seen:
        original = next(item for item in body["messages"] if item["role"] == "user")
        assert original["content"] == [
            {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}
        ]
        assert "input_modalities" not in body


@pytest.mark.parametrize("audio_only", [True, False])
async def test_public_audio_history_and_restore(audio_only: bool) -> None:
    content: list[InputText | InputAudio] = [
        {"type": "input_audio", "audio_url": "data:audio/wav;base64,UklGRg=="}
    ]
    if not audio_only:
        content = [
            {"type": "input_text", "text": "before"},
            *content,
            {"type": "input_text", "text": "after"},
        ]
    message: UserMessage = {"role": "user", "content": content}
    seen: list[dict[str, Any]] = []

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request.body)
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            )

    async with NativeProvider(
        base_url="https://example.invalid/v1", model_backend=Model()
    ) as provider:
        agent = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={"input_modalities": ["text", "audio"]},
        )
        async with agent.session() as session:
            await (await session.submit(message)).get_result()
            items = [item async for item in session.items()]
            original = next(
                item for item in items if item["type"] == "message" and item.get("role") == "user"
            )
            assert original["content"] == content
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            items = [item async for item in restored.items()]
            original = next(
                item for item in items if item["type"] == "message" and item.get("role") == "user"
            )
            assert original["content"] == content


@pytest.mark.parametrize("audio_only", [True, False])
async def test_initial_audio_sources_order_and_restore(audio_only: bool) -> None:
    content: list[InputText | InputAudio] = [
        {"type": "input_audio", "audio_url": "data:audio/wav;base64,UklGRg=="}
    ]
    if not audio_only:
        content = [
            {"type": "input_text", "text": "before"},
            *content,
            {"type": "input_text", "text": "after"},
        ]
    message: UserMessage = {"role": "user", "content": content}
    seen: list[dict[str, Any]] = []

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request.body)
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            )

    async with NativeProvider(
        base_url="https://example.invalid/v1", model_backend=Model()
    ) as provider:
        agent = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={"input_modalities": ["text", "audio"]},
        )
        async with agent.session(initial_messages=[message]) as session:
            snapshot = await session.model_context()
            original = next(
                item
                for item in snapshot.items
                if item["type"] == "message" and item.get("role") == "user"
            )
            assert original["content"] == content
            assert seen == []
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            await (await restored.submit("continue")).get_result()
            original = next(item for item in seen[0]["input"] if item.get("role") == "user")
            assert original["content"] == content


async def test_malformed_seeded_audio_rejects_before_model_effects() -> None:
    for part in [
        {"type": "input_audio"},
        {"type": "input_audio", "audio_url": ""},
        {"type": "input_audio", "audio_url": None},
        {"type": "input_audio", "audio_url": 42},
        {"type": "input_audio", "audio_url": "source", "file_id": "other"},
    ]:
        calls = 0

        class Model:
            def complete(self, request: ModelRequest) -> ModelResponse:
                nonlocal calls
                calls += 1
                raise AssertionError("unexpected model effect")

        async with NativeProvider(
            base_url="https://example.invalid/v1", model_backend=Model()
        ) as provider:
            agent = Agent(
                instructions="Answer.",
                provider=provider,
                model_settings={"input_modalities": ["text", "audio"]},
            )
            message: Any = {"role": "user", "content": [part]}
            with pytest.raises((TypeError, ValueError)):
                async with agent.session(initial_messages=[message]):
                    raise AssertionError("unexpected admitted seed")
            assert calls == 0
