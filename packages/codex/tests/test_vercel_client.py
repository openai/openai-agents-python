"""Check the application recipe with a ai 6.0.315 client, without a browser."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, function_tool
from examples.codex.vercel_stream import stream_turn
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_native_agent import provider

CLIENT = Path(__file__).parent / "vercel-client"


@pytest.fixture
def node() -> str:
    executable = shutil.which("node")
    if executable is None or not (CLIENT / "node_modules/ai/package.json").is_file():
        if os.environ.get("AGENTS_REQUIRE_VERCEL_CLIENT") == "1":
            pytest.fail(
                "The Vercel client check requires Node 22 and npm ci in "
                "packages/codex/tests/vercel-client"
            )
        pytest.skip("optional: Node 22 and npm ci in packages/codex/tests/vercel-client")
    return executable


def read_client(node: str, body: bytes) -> dict[str, Any]:
    result = subprocess.run(
        [node, str(CLIENT / "read-stream.mjs")],
        input=json.dumps({"body": body.decode("utf-8")}),
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


async def test_vercel_client_reads_native_text_and_transient_progress(
    responses: ResponsesFixture, node: str
) -> None:
    effects: list[str] = []

    @function_tool
    def lookup(secret: str) -> str:
        effects.append(secret)
        return "synthetic-private-result"

    responses.respond(
        *text_response("native_intro", "Checking 雪\n"),
        function_call("lookup", {"secret": "synthetic-private-argument"}, "native_lookup"),
    )
    responses.respond(*text_response("native_final", "Found ", "雪."))
    async with (
        timeout(20),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Read the synthetic record")
        body = b"".join([frame async for frame in stream_turn(turn, session=session)])
        assert (await turn.get_result()).final_output == "Found 雪."
    result = read_client(node, body)
    assert result["error"] is None
    assert result["chunks"][-1] == {"type": "finish"}
    assert [chunk["id"] for chunk in result["chunks"] if chunk["type"] == "text-start"] == [
        "native_intro",
        "native_final",
    ]
    final = result["messages"][-1]
    assert final["role"] == "assistant"
    assert [part["text"] for part in final["parts"] if part["type"] == "text"] == [
        "Checking 雪\n",
        "Found 雪.",
    ]
    progress = [chunk for chunk in result["chunks"] if chunk["type"] == "data-tool-progress"]
    assert {chunk["data"]["type"] for chunk in progress} == {"tool_started", "tool_finished"}
    assert all(chunk["transient"] is True for chunk in progress)
    assert all(part["type"] == "text" for part in final["parts"])
    assert effects == ["synthetic-private-argument"]
    assert "synthetic-private" not in json.dumps(result)


async def test_vercel_client_reports_observation_loss_without_success(
    responses: ResponsesFixture, node: str
) -> None:
    responses.respond(*text_response("native_lost", "old"))
    responses.respond(*text_response("native_flood", *(["雪"] * 350)))
    async with (
        timeout(20),
        Agent(instructions="Reply.", provider=provider(responses)).session() as session,
    ):
        old = await session.submit("First")
        assert (await old.get_result()).final_output == "old"
        later = await session.submit("Fill the bounded observation buffer")
        assert (await later.get_result()).final_output == "雪" * 350
        body = b"".join([frame async for frame in stream_turn(old, session=session)])
    result = read_client(node, body)
    assert result["error"] == "resync_required"
    assert any(chunk["type"] == "error" for chunk in result["chunks"])
    assert not any(chunk["type"] == "finish" for chunk in result["chunks"])
    assert not any(message["parts"] for message in result["messages"])
