from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agents import Agent
from agents.extensions.a2a import (
    A2AClient,
    A2AError,
    A2AServer,
    Message,
    Task,
    TaskState,
    TextPart,
)
from agents.extensions.a2a.models import message_from_text
from agents.tool_context import ToolContext
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


def build_server(text: str = "Paris") -> A2AServer:
    model = FakeModel()
    model.set_next_output([get_text_message(text)])
    agent = Agent(name="Assistant", instructions="You are helpful.", model=model)
    return A2AServer(agent, url="http://testserver/")


def send_body(text: str, request_id: str = "1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}},
    }


# -- Models --------------------------------------------------------------------


def test_message_roundtrips_camelcase() -> None:
    msg = Message(role="user", parts=[TextPart(text="hi")], context_id="c1")
    dumped = msg.model_dump(by_alias=True, exclude_none=True)
    assert dumped["contextId"] == "c1"
    assert dumped["parts"][0] == {"kind": "text", "text": "hi"}

    parsed = Message.model_validate(dumped)
    assert parsed.context_id == "c1"
    assert isinstance(parsed.parts[0], TextPart)


def test_message_from_text_helper() -> None:
    msg = message_from_text("hello")
    assert msg.role == "agent"
    assert msg.parts[0].text == "hello"  # type: ignore[union-attr]


# -- Server: agent card --------------------------------------------------------


def test_agent_card_served() -> None:
    server = build_server()
    client = TestClient(server.app)

    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Assistant"
    assert card["protocolVersion"]
    assert card["capabilities"]["streaming"] is True
    assert card["skills"][0]["name"] == "Assistant"

    # Legacy path serves the same card.
    assert client.get("/.well-known/agent.json").json() == card


# -- Server: message/send ------------------------------------------------------


def test_message_send_returns_completed_task() -> None:
    server = build_server("Paris")
    client = TestClient(server.app)

    resp = client.post("/", json=send_body("What is the capital of France?"))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == "1"
    result = payload["result"]
    assert result["kind"] == "task"
    assert result["status"]["state"] == "completed"
    assert result["artifacts"][0]["parts"][0]["text"] == "Paris"


def test_tasks_get_after_send() -> None:
    server = build_server("Paris")
    client = TestClient(server.app)

    task_id = client.post("/", json=send_body("hi")).json()["result"]["id"]
    resp = client.post(
        "/", json={"jsonrpc": "2.0", "id": "2", "method": "tasks/get", "params": {"id": task_id}}
    )
    assert resp.json()["result"]["id"] == task_id


def test_tasks_get_missing_returns_error() -> None:
    server = build_server()
    client = TestClient(server.app)
    resp = client.post(
        "/", json={"jsonrpc": "2.0", "id": "9", "method": "tasks/get", "params": {"id": "nope"}}
    )
    assert resp.json()["error"]["code"] == -32001


def test_unknown_method_returns_error() -> None:
    server = build_server()
    client = TestClient(server.app)
    resp = client.post("/", json={"jsonrpc": "2.0", "id": "1", "method": "bogus/method"})
    assert resp.json()["error"]["code"] == -32601


# -- Server: message/stream ----------------------------------------------------


def test_message_stream_emits_status_and_artifact() -> None:
    server = build_server("Paris")
    client = TestClient(server.app)

    body = send_body("capital of France?")
    body["method"] = "message/stream"
    resp = client.post("/", json=body)
    assert resp.status_code == 200

    events = [
        json.loads(line[len("data:") :].strip())["result"]
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    kinds = [e["kind"] for e in events]
    assert "status-update" in kinds
    assert "artifact-update" in kinds

    # Reassemble streamed artifact text.
    streamed = "".join(
        part["text"]
        for e in events
        if e["kind"] == "artifact-update"
        for part in e["artifact"]["parts"]
    )
    assert "Paris" in streamed

    # The final event is a terminal completed status update.
    final = events[-1]
    assert final["kind"] == "status-update"
    assert final["final"] is True
    assert final["status"]["state"] == "completed"


# -- Client --------------------------------------------------------------------


def _client_for(server: A2AServer) -> A2AClient:
    transport = httpx.ASGITransport(app=server.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return A2AClient("http://test/", httpx_client=http)


async def test_client_get_card_and_send() -> None:
    server = build_server("Paris")
    client = _client_for(server)
    try:
        card = await client.get_agent_card()
        assert card.name == "Assistant"

        result = await client.send_message("capital of France?")
        assert isinstance(result, Task)
        assert result.status.state == TaskState.COMPLETED
    finally:
        await client._client.aclose()


async def test_client_as_tool_delegates() -> None:
    server = build_server("Paris")
    client = _client_for(server)
    try:
        tool = client.as_tool(tool_name="ask_remote")
        assert tool.name == "ask_remote"
        ctx: ToolContext = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=""
        )
        output = await tool.on_invoke_tool(ctx, json.dumps({"input": "capital of France?"}))
        assert output == "Paris"
    finally:
        await client._client.aclose()


async def test_client_raises_on_error() -> None:
    server = build_server()
    client = _client_for(server)
    try:
        with pytest.raises(A2AError):
            await client.get_task("does-not-exist")
    finally:
        await client._client.aclose()
