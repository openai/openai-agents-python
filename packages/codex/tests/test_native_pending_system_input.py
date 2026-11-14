import asyncio
import json
import time
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from agents import (
    Agent,
    CustomStorage,
    EncryptedSessionStore,
    FunctionTool,
    NativeProvider,
    SessionState,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("role", ["system", "developer"])
async def test_staged_caller_instruction_stays_data_and_keeps_wire_role(responses, role):
    lookup = FunctionTool(
        name="lookup", description="Read.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "one"}, "pending-system"))
    responses.respond(assistant_output("continued"))
    marker = "CALLER_INSTRUCTION_LOOKALIKE"
    item = {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": marker}],
        "internal_chat_message_metadata_passthrough": {
            "content_item_kinds": ["sdk_application.instructions"]
        },
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-system-staging", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            instructions="CURRENT_APP_SYSTEM_SCOPE",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
        )
        async with agent.session() as source:
            await source.submit("initial")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        staged = SessionState.from_json(saved.with_pending_input([item]).to_json())
        assert staged.pending_input == [item]
        async with agent.session(state=staged) as restored:
            pending = await next_request(restored.interactions(), lookup)
            await pending.respond("accepted")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
            assert marker in json.dumps((await restored.history_snapshot()).items)
    assert len(responses.requests) == 2
    assert any(
        x.get("role") == role and marker in json.dumps(x) for x in responses.requests[1]["input"]
    )
    assert "CURRENT_APP_SYSTEM_SCOPE" in json.dumps(responses.requests[1])


@pytest.mark.parametrize("ttl", [None, 0])
@pytest.mark.parametrize("cold", [False, True])
async def test_new_caller_system_import_uses_native_retention_without_hidden_authority(
    responses, ttl, cold
):
    backend = EncryptedSessionStore(
        MemorySessionStore(), encryption_key="synthetic-system-retention", ttl=ttl
    )
    agent_args = {"instructions": "CURRENT_APP_SYSTEM_SCOPE", "model": "gpt-5.5"}
    marker = "CALLER_SYSTEM_IMPORT"
    item = {
        "type": "message",
        "role": "system",
        "content": [{"type": "input_text", "text": marker}],
    }
    responses.respond(assistant_output("current answer"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-system-retention", supports_websockets=False
        ) as provider,
        AsyncExitStack() as leases,
    ):
        agent = Agent(provider=provider, **agent_args)
        source = await leases.enter_async_context(agent.session(storage=CustomStorage(backend)))
        try:
            snapshot = await source.history_snapshot()
            await source.transact_history(expected_revision=snapshot.revision, keep=0, items=[item])
            identity = source.id
            if cold:
                await source.aclose()
                source = await leases.enter_async_context(
                    agent.session(storage=CustomStorage(backend), resume=identity)
                )
            if ttl is not None:
                tick = int(time.time()) + 1
                while time.time() < tick:
                    await asyncio.sleep(0.01)
            assert (marker in json.dumps((await source.model_context()).items)) == (ttl is None)
            assert (
                await (await source.submit("continue")).get_result()
            ).final_output == "current answer"
            assert item in (await source.history_snapshot()).items
        finally:
            await source.aclose()
    assert (marker in json.dumps(responses.requests[0]["input"])) == (ttl is None)
    assert "CURRENT_APP_SYSTEM_SCOPE" in json.dumps(responses.requests[0])


async def test_old_audit_only_system_original_does_not_gain_projection_on_restore(responses):
    fixture = Path(__file__).with_name("fixtures") / "native-old-system-audit-state.json"
    state = SessionState.from_json(fixture.read_text())
    marker = "OLD_AUDIT_ONLY_SYSTEM_STAYS_INERT"
    responses.respond(assistant_output("restored"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-old-system", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="CURRENT_APP_SYSTEM_SCOPE", model="gpt-5.5", provider=provider)
        async with agent.session(state=state) as restored:
            assert marker in json.dumps((await restored.history_snapshot()).items)
            assert marker not in json.dumps((await restored.model_context()).items)
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "restored"
    assert marker not in json.dumps(responses.requests[0])
    assert "CURRENT_APP_SYSTEM_SCOPE" in json.dumps(responses.requests[0])
