"""Integration tests for AgentServer against a real OpenAI model.

These are skipped unless a real ``OPENAI_API_KEY`` is set (the test suite
otherwise injects a dummy ``test_key``), and are marked
``allow_call_model_methods`` so the real-model guard in ``conftest`` lets them
through.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from agents import Agent
from agents.extensions.server import AgentServer

_KEY = os.getenv("OPENAI_API_KEY")
pytestmark = [
    pytest.mark.allow_call_model_methods,
    pytest.mark.skipif(
        not _KEY or _KEY == "test_key",
        reason="requires a real OPENAI_API_KEY",
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
