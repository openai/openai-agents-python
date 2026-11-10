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
            model_settings={"tool_choice": ToolRef("lookup")},
        ).run(message)
    assert result.final_output == "ok"
    assert effects == ["lookup"]
    assert len(seen) == 2
    for body in seen:
        original = next(item for item in body["input"] if item.get("role") == "user")
        assert original["content"] == content
    assert any(item.get("type") == "function_call_output" for item in seen[1]["input"])
