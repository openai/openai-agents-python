"""Manual suspension keeps checked persistence outside the bounded tool join."""

import asyncio
import json
import time

from _agents_codex.input import _native_input
from agents import Agent, CustomStorage, _native
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_custom_storage_manual import lookup_tool, provider_for
from tests.test_native_manual_tools import next_request


async def test_manual_capture_waits_for_slow_checked_fences(responses):
    class Store(MemorySessionStore):
        capturing = False

        def flush(self, session_id, token):
            if self.capturing:
                # A real storage fence can exceed Core's cooperative tool join.
                time.sleep(0.15)
            return super().flush(session_id, token)

    backend = Store()
    lookup = lookup_tool()
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    responses.respond(assistant_output("done"))
    async with timeout(30), provider_for(responses.url) as provider:
        agent = Agent(
            instructions="Read exactly once.", model="gpt-5.5", provider=provider, tools=[lookup]
        )
        async with agent.session(storage=CustomStorage(backend)) as source:
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            backend.capturing = True
            try:
                saved = await source.suspend()
            finally:
                backend.capturing = False
            token = backend.acquire(source.id)
            try:
                assert backend.read(source.id, token) is not None
            finally:
                backend.release(source.id, token)
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("answer-A")
            assert restored.restored_turn is not None
            assert (await restored.restored_turn.get_result()).final_output == "done"
    assert len(responses.requests) == 2


async def test_generic_suspend_before_model_response(responses):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold(event):
        if event["type"] == "response.created":
            entered.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(assistant_output("held"))
    config = {
        "model": "gpt-5.5",
        "model_provider": "native_agent",
        "model_providers.native_agent": {
            "name": "Synthetic provider",
            "base_url": responses.url,
            "wire_api": "responses",
            "requires_openai_auth": True,
            "supports_websockets": False,
        },
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
    }
    async with timeout(20):
        engine = await _native.create_engine(json.dumps(config), api_key="synthetic")
        try:
            thread = await engine.start_thread()
            try:
                await thread.call("start_turn", json.dumps({"input": _native_input("wait")}))
                await entered.wait()
                result = json.loads(await thread.call("suspend_turn_and_shutdown"))
                assert result["status"] == "suspended"
            finally:
                release.set()
                await thread.close()
        finally:
            await engine.close()
