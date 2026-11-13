import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import Agent, ConversationsHistory, FunctionTool, InteractionRequired, NativeProvider
from agents.state import SessionState
from agents.telemetry import TraceOptions, configure
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_conversations_history import endpoint as endpoint


@pytest.mark.parametrize("case", ["session", "manual"])
def test_native_prompt_cache_trace_affinity(case: str, tmp_path: Path) -> None:
    # Native telemetry must be configured before the first SDK client in this process.
    env = {**os.environ, "CODEX_HOME": str(tmp_path)}
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; "
            "from tests.test_native_prompt_cache_affinity import _trace_affinity_case; "
            "asyncio.run(asyncio.wait_for(_trace_affinity_case(sys.argv[1]), 30))",
            case,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _trace_affinity_case(case: str) -> None:
    await configure()
    fixture = ResponsesFixture()
    application = web.Application()
    application.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(application) as server:
        fixture.url = str(server.make_url("/v1"))
        if case == "session":
            await _session_affinity(fixture)
        else:
            assert case == "manual"
            await _manual_affinity(fixture)


async def _session_affinity(responses: ResponsesFixture) -> None:
    for index in range(6):
        responses.respond(assistant_output(f"answer {index}"))

    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-loopback",
        supports_websockets=False,
    ) as provider:
        agent = Agent(
            model="fixture-unlisted-cache-affinity", instructions="Answer.", provider=provider
        )
        async with agent.session() as session:
            for index in range(2):
                turn = await session.submit(f"Original input {index}")
                assert (await turn.get_result()).final_output == f"answer {index}"
            saved = await session.save_state()
        async with agent.session(state=saved) as restored:
            turn = await restored.submit("Restored input")
            assert (await turn.get_result()).final_output == "answer 2"
        for index, group in enumerate(
            ("synthetic-group-a", "synthetic-group-a", "synthetic-group-b"), 3
        ):
            result = await agent.run(
                "Independent input", trace_options=TraceOptions(group_id=group)
            )
            assert result.final_output == f"answer {index}"

    requests = responses.requests
    assert len(requests) == 6
    assert "Original input 0" in json.dumps(requests[2]["input"])
    keys = [request.get("prompt_cache_key") for request in requests]
    assert all(isinstance(key, str) and key for key in keys)
    assert keys[0] == keys[1]
    assert keys[0] == keys[2]
    assert keys[3] == keys[4]
    assert keys[4] != keys[5]


async def test_current_native_conversation_affinity(responses, endpoint) -> None:
    _peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    for answer in ("first answer", "second answer"):
        responses.respond(assistant_output(answer))
    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-loopback",
        supports_websockets=False,
    ) as provider:
        agent = Agent(model="gpt-5", instructions="Answer.", provider=provider)
        assert (
            await history.run(agent, "original conversation input")
        ).final_output == "first answer"
        assert (
            await history.run(agent, "continued conversation input")
        ).final_output == "second answer"
    assert len(responses.requests) == 2
    assert "original conversation input" in json.dumps(responses.requests[1]["input"])
    keys = [request.get("prompt_cache_key") for request in responses.requests]
    assert all(isinstance(key, str) and key for key in keys)
    assert keys[0] == keys[1]


class LookupArgs(BaseModel):
    key: str


async def _manual_affinity(responses: ResponsesFixture) -> None:
    responses.respond(function_call("lookup", {"key": "fixture"}, "cache-affinity-manual"))
    responses.respond(assistant_output("resumed"))
    lookup = FunctionTool(
        name="lookup", description="Lookup.", input_type=LookupArgs, output_type=str
    )
    initial_group = "synthetic-original-group"
    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-loopback",
        supports_websockets=False,
    ) as provider:
        agent = Agent(model="gpt-5", instructions="Use lookup.", provider=provider, tools=[lookup])
        with pytest.raises(InteractionRequired) as pending:
            await agent.run("Look it up.", trace_options=TraceOptions(group_id=initial_group))
        assert pending.value.state is not None
        saved = SessionState.from_json(pending.value.state.to_json())
        async with agent.session(
            state=saved, trace_options=TraceOptions(group_id="synthetic-new-group")
        ) as session:
            async for event in session.interactions():
                if request := lookup.match(event):
                    assert request.call_id == "cache-affinity-manual"
                    await request.respond("answer")
                    break
            else:
                pytest.fail("Missing restored manual request")
            restored = session.restored_turn
            assert restored is not None
            assert (await restored.get_result()).final_output == "resumed"

    keys = [request.get("prompt_cache_key") for request in responses.requests]
    assert len(keys) == 2 and all(isinstance(key, str) and key for key in keys)
    expected = "agents-sdk:group:" + hashlib.sha256(initial_group.encode()).hexdigest()[:32]
    assert keys == [expected, expected]
