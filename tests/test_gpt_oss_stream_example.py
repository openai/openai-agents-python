from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/reasoning_content/gpt_oss_stream.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("external_key", [None, ""], ids=["missing", "empty"])
async def test_external_key_is_required_before_client_construction(monkeypatch, external_key):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-first-party-sentinel")
    if external_key is None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", external_key)
    constructions = []

    def forbid_client(**kwargs):
        constructions.append(kwargs)
        raise AssertionError("The example must reject the missing vendor key first.")

    monkeypatch.setattr(openai, "AsyncOpenAI", forbid_client)
    namespace = runpy.run_path(str(EXAMPLE), run_name="external_key_import_check")
    assert constructions == []
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        await namespace["main"]()
    assert constructions == []


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_explicit_external_key_reaches_only_the_owned_mock_transport(monkeypatch, capsys):
    vendor_key = "synthetic-external-provider-sentinel"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-first-party-sentinel")
    monkeypatch.setenv("OPENROUTER_API_KEY", vendor_key)
    requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {vendor_key}"
        assert json.loads(request.content)["model"] == "openai/gpt-oss-20b"
        base: dict[str, Any] = {
            "id": "chatcmpl_fixture",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "openai/gpt-oss-20b",
        }
        chunks = [
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "Recursion repeats a smaller task.",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n",
        )

    real_client = openai.AsyncOpenAI
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as transport:
        clients = []

        def client(**kwargs):
            value = real_client(**kwargs, http_client=transport)
            clients.append(value)
            return value

        monkeypatch.setattr(openai, "AsyncOpenAI", client)
        namespace = runpy.run_path(str(EXAMPLE), run_name="external_key_control")
        namespace["main"].__globals__["set_tracing_disabled"] = lambda disabled: None
        await namespace["main"]()
        assert len(requests) == 1
        assert "Recursion repeats a smaller task." in capsys.readouterr().out
        for value in clients:
            await value.close()
