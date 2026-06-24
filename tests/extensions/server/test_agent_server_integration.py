"""Integration tests for AgentServer against a real OpenAI model.

These make real API calls, so they are opt-in: they only run when
``OPENAI_INTEGRATION_TESTS=1`` is set (with a real ``OPENAI_API_KEY``). CI never
sets that flag — and injects a fake key — so these are skipped there. They are
marked ``allow_call_model_methods`` so the real-model guard in ``conftest`` lets
them through when explicitly enabled.

Run locally with::

    OPENAI_INTEGRATION_TESTS=1 OPENAI_API_KEY=sk-... uv run pytest \
        tests/extensions/server/test_agent_server_integration.py
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from agents import Agent
from agents.extensions.server import AgentServer

pytestmark = [
    pytest.mark.allow_call_model_methods,
    pytest.mark.skipif(
        os.getenv("OPENAI_INTEGRATION_TESTS") != "1",
        reason="set OPENAI_INTEGRATION_TESTS=1 (with a real OPENAI_API_KEY) to run",
    ),
]

_MODEL = "gpt-4o-mini"


def _server() -> AgentServer:
    agent = Agent(
        name="Geography",
        instructions="Answer with only the city name, nothing else.",
        model=_MODEL,
    )
    return AgentServer(agent)


def test_invoke_real() -> None:
    client = TestClient(_server().app)
    resp = client.post("/invoke", json={"input": "What is the capital of France?"})
    assert resp.status_code == 200
    assert "paris" in str(resp.json()["output"]).lower()


def test_stream_real() -> None:
    client = TestClient(_server().app)
    resp = client.post("/stream", json={"input": "What is the capital of France?"})
    assert resp.status_code == 200
    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    assert events[-1]["type"] == "done"
    assert "paris" in str(events[-1]["output"]).lower()
