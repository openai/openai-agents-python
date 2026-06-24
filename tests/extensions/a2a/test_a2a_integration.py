"""Integration tests for the A2A extension against a real OpenAI model.

These make real API calls, so they are opt-in: they only run when
``OPENAI_INTEGRATION_TESTS=1`` is set (with a real ``OPENAI_API_KEY``). CI never
sets that flag — and injects a fake key — so these are skipped there. They are
marked ``allow_call_model_methods`` so the real-model guard in ``conftest`` lets
them through when explicitly enabled.

Run locally with::

    OPENAI_INTEGRATION_TESTS=1 OPENAI_API_KEY=sk-... uv run pytest \
        tests/extensions/a2a/test_a2a_integration.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from agents import Agent
from agents.extensions.a2a import A2AClient, A2AServer, Task, text_from_message

pytestmark = [
    pytest.mark.allow_call_model_methods,
    pytest.mark.skipif(
        os.getenv("OPENAI_INTEGRATION_TESTS") != "1",
        reason="set OPENAI_INTEGRATION_TESTS=1 (with a real OPENAI_API_KEY) to run",
    ),
]

_MODEL = "gpt-4o-mini"


def _build_server() -> A2AServer:
    agent = Agent(
        name="Geography",
        instructions="Answer with only the city name, nothing else.",
        model=_MODEL,
    )
    return A2AServer(agent, url="http://test/")


def _client_for(server: A2AServer) -> A2AClient:
    transport = httpx.ASGITransport(app=server.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return A2AClient("http://test/", httpx_client=http)


async def test_a2a_real_send() -> None:
    client = _client_for(_build_server())
    try:
        result = await client.send_message("What is the capital of France?")
        assert isinstance(result, Task)
        assert result.status.message is not None
        assert "paris" in text_from_message(result.status.message).lower()
    finally:
        await client._client.aclose()


async def test_a2a_real_stream() -> None:
    client = _client_for(_build_server())
    try:
        chunks: list[str] = []
        async for update in client.stream_message("What is the capital of France?"):
            if update.get("kind") == "artifact-update":
                for part in update["artifact"]["parts"]:
                    chunks.append(part.get("text", ""))
        assert "paris" in "".join(chunks).lower()
    finally:
        await client._client.aclose()
