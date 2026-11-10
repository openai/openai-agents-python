"""Audio input and named function selection through existing public adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI

from _agents_codex.input import _native_input, _response_content
from agents import ChatCompletionsModel, ModelRequest
from agents.input import InputAudio, UserMessage


def test_ordered_audio_capture_and_projection_without_transport() -> None:
    audio = InputAudio(type="input_audio", audio_url="https://example.test/audio.ogg")
    message: UserMessage = {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "before"},
            audio,
            {"type": "input_text", "text": "after"},
        ],
    }
    captured = _native_input(message)
    assert [part["type"] for part in captured] == ["text", "audio", "text"]
    assert captured[1]["audio_url"] == audio["audio_url"]
    audio["audio_url"] = "changed"
    assert captured[1]["audio_url"] == "https://example.test/audio.ogg"
    assert _response_content([{"type": "audio", "audio_url": "data:audio/ogg;base64,Tw=="}]) == [
        {"type": "input_audio", "audio_url": "data:audio/ogg;base64,Tw=="}
    ]


@pytest.mark.parametrize("mime,format", [("wav", "wav"), ("mpeg", "mp3"), (None, None)])
async def test_real_openai_chat_audio_and_named_choice(
    mime: str | None, format: str | None
) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
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
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        async with AsyncOpenAI(api_key="fixture", http_client=transport) as client:
            body: dict[str, Any] = {
                "model": "fixture",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "before"},
                            {
                                "type": "input_audio",
                                "audio_url": f"data:audio/{mime};base64,UklGRg==",
                            },
                            {"type": "input_text", "text": "after"},
                        ],
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                "tool_choice": {"type": "function", "name": "lookup", "namespace": "functions"},
            }
            if mime is None:
                body["input"][0]["content"].pop(1)
            await ChatCompletionsModel(client).complete(ModelRequest(body, None, "session", "turn"))
    assert seen[0]["messages"][0]["content"] == [
        {"type": "text", "text": "before"},
        *(
            [{"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": format}}]
            if mime is not None
            else []
        ),
        {"type": "text", "text": "after"},
    ]
    assert seen[0]["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert body["tool_choice"]["namespace"] == "functions"


@pytest.mark.parametrize(
    "bad", ["https://example.test/private.wav", "data:audio/ogg;base64,UklGRg=="]
)
async def test_unrepresentable_chat_audio_rejects_before_client_and_releases_locals(
    bad: str,
) -> None:
    class Client:
        @property
        def chat(self) -> object:
            pytest.fail("client accessed before unsupported audio rejection")

    try:
        await ChatCompletionsModel(Client()).complete(
            ModelRequest(
                {
                    "model": "fixture",
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_audio", "audio_url": bad}],
                        }
                    ],
                },
                None,
                "session",
                "turn",
            )
        )
    except ValueError as error:
        trace = error.__traceback__
        while trace:
            if trace.tb_frame.f_code is ChatCompletionsModel.complete.__code__:
                assert not (
                    {
                        "request",
                        "audio_data",
                        "audio_format",
                        "audio_header",
                        "audio_separator",
                        "part",
                        "messages",
                    }
                    & set(trace.tb_frame.f_locals)
                )
            trace = trace.tb_next
    else:
        pytest.fail("unsupported Chat audio accepted")


@pytest.mark.parametrize("boundary", ["foreign", "missing", "empty", "catalogue"])
async def test_named_selector_ownership_before_client(boundary: str) -> None:
    class Client:
        @property
        def chat(self) -> object:
            pytest.fail("client accessed before selector rejection")

    body: dict[str, Any] = {
        "model": "fixture",
        "input": [],
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": {
            "type": "function",
            "name": "other" if boundary == "missing" else "lookup",
            "namespace": "other" if boundary == "foreign" else "functions",
        },
    }
    if boundary == "empty":
        body["tools"] = []
    elif boundary == "catalogue":
        body["tools"][0]["namespace"] = "other"
    with pytest.raises(ValueError, match="offered|namespace"):
        await ChatCompletionsModel(Client()).complete(ModelRequest(body, None, "session", "turn"))


@pytest.mark.parametrize("adapter", ["litellm", "any_llm"])
async def test_real_borrowed_library_audio_and_named_choice(adapter: str, monkeypatch) -> None:
    from functools import partial
    from types import SimpleNamespace

    from agents import AnyLLMModel, LitellmModel

    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
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
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    body: dict[str, Any] = {
        "model": "gpt-4.1",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_audio", "audio_url": "data:audio/wav;base64,UklGRg=="}],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": {"type": "function", "name": "lookup"},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        if adapter == "litellm":
            monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            library = pytest.importorskip("litellm")
            monkeypatch.setattr(library, "telemetry", False)
            async with AsyncOpenAI(api_key="fixture", http_client=transport) as client:
                backend = LitellmModel(
                    SimpleNamespace(acompletion=partial(library.acompletion, client=client)),
                    "openai/gpt-4.1",
                )
                await backend.complete(ModelRequest(body, None, "session", "turn"))
        else:
            library = pytest.importorskip("any_llm")
            borrowed = library.AnyLLM.create("openai", api_key="fixture", http_client=transport)
            try:
                await AnyLLMModel(borrowed, "gpt-4.1").complete(
                    ModelRequest(body, None, "session", "turn")
                )
            finally:
                await borrowed.client.close()
    assert len(seen) == 1
    assert seen[0]["messages"][0]["content"] == [
        {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}
    ]
    assert seen[0]["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
