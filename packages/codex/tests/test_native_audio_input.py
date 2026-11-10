"""Native audio admission preserves mixed-content order through tool continuation."""

from typing import Any

from agents import Agent, ModelRequest, ModelResponse, NativeProvider, ToolRef, function_tool
from agents.input import InputAudio, InputText, UserMessage
from tests._asyncio import timeout


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
        result = await Agent(
            model="custom-model",
            instructions="Use lookup once.",
            tools=[lookup],
            provider=provider,
            model_settings={
                "tool_choice": ToolRef("lookup"),
                "input_modalities": ["text", "audio"],
            },
        ).run(message)
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
