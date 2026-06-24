from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agents import Agent
from agents.extensions.server import AgentServer
from agents.memory.sqlite_session import SQLiteSession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


def build_server(
    *,
    api_key: str | None = None,
    sessions: bool = False,
    texts: tuple[str, ...] = ("Paris",),
) -> AgentServer:
    model = FakeModel()
    model.add_multiple_turn_outputs([[get_text_message(t)] for t in texts])
    agent = Agent(name="Assistant", instructions="You are helpful.", model=model)
    factory = (lambda tid: SQLiteSession(tid)) if sessions else None
    return AgentServer(agent, session_factory=factory, api_key=api_key)


# -- Basics --------------------------------------------------------------------


def test_health() -> None:
    client = TestClient(build_server().app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_invoke_stateless() -> None:
    client = TestClient(build_server(texts=("Paris",)).app)
    resp = client.post("/invoke", json={"input": "capital of France?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"] == "Paris"
    assert body["thread_id"] is None


def test_invoke_requires_string_input() -> None:
    client = TestClient(build_server().app)
    resp = client.post("/invoke", json={"input": 123})
    assert resp.status_code == 422


# -- Streaming -----------------------------------------------------------------


def test_stream_emits_text_and_done() -> None:
    client = TestClient(build_server(texts=("Paris",)).app)
    resp = client.post("/stream", json={"input": "capital of France?"})
    assert resp.status_code == 200

    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    types = [e["type"] for e in events]
    assert "text_delta" in types
    assert events[-1]["type"] == "done"
    assert events[-1]["output"] == "Paris"


# -- Threads / sessions --------------------------------------------------------


def test_threads_disabled_without_factory() -> None:
    client = TestClient(build_server(sessions=False).app)
    assert client.post("/invoke", json={"input": "hi", "thread_id": "t1"}).status_code == 400
    assert client.get("/threads/t1").status_code == 400


def test_thread_persistence_and_clear() -> None:
    client = TestClient(build_server(sessions=True, texts=("Paris", "Lyon")).app)

    first = client.post("/invoke", json={"input": "capital of France?", "thread_id": "t1"})
    assert first.json()["output"] == "Paris"

    items = client.get("/threads/t1").json()["items"]
    assert len(items) >= 2  # user message + assistant reply

    # A second turn on the same thread continues the conversation.
    second = client.post("/invoke", json={"input": "another city?", "thread_id": "t1"})
    assert second.json()["output"] == "Lyon"
    assert len(client.get("/threads/t1").json()["items"]) > len(items)

    # Clearing drops the history.
    assert client.delete("/threads/t1").json()["status"] == "cleared"
    assert client.get("/threads/t1").json()["items"] == []


# -- Auth ----------------------------------------------------------------------


def test_auth_rejects_missing_key() -> None:
    client = TestClient(build_server(api_key="secret").app)
    assert client.post("/invoke", json={"input": "hi"}).status_code == 401


def test_auth_accepts_bearer_and_header() -> None:
    server = build_server(api_key="secret", texts=("Paris", "Paris"))
    client = TestClient(server.app)
    bearer = client.post(
        "/invoke", json={"input": "hi"}, headers={"Authorization": "Bearer secret"}
    )
    assert bearer.status_code == 200
    api_header = client.post("/invoke", json={"input": "hi"}, headers={"X-API-Key": "secret"})
    assert api_header.status_code == 200


# -- run() guard ---------------------------------------------------------------


def test_run_refuses_public_bind_without_auth() -> None:
    server = build_server()
    with pytest.raises(ValueError):
        server.run(host="0.0.0.0")


# -- Codex review regressions --------------------------------------------------


def test_invoke_preserves_structured_output() -> None:
    # A structured (Pydantic) output must come back as JSON data, not str(repr).
    class Answer(BaseModel):
        value: int
        label: str

    model = FakeModel()
    model.set_next_output([get_text_message('{"value": 42, "label": "answer"}')])
    agent = Agent(name="Counter", instructions="x", model=model, output_type=Answer)
    client = TestClient(AgentServer(agent).app)

    output = client.post("/invoke", json={"input": "number?"}).json()["output"]
    assert output == {"value": 42, "label": "answer"}
    assert isinstance(output, dict)


def test_invoke_rejects_non_string_thread_id() -> None:
    client = TestClient(build_server(sessions=True).app)
    resp = client.post("/invoke", json={"input": "hi", "thread_id": 123})
    assert resp.status_code == 422


def test_stream_rejects_non_string_thread_id() -> None:
    client = TestClient(build_server(sessions=True).app)
    resp = client.post("/stream", json={"input": "hi", "thread_id": 123})
    assert resp.status_code == 422
