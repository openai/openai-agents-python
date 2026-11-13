"""An idle canonical session can snapshot into isolated native memory."""

import json
import os

import pytest

from agents import (
    Agent,
    CapabilityError,
    CustomStorage,
    EncryptedSessionStore,
    NativeProvider,
    SQLiteSessionStore,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="SQLiteSessionStore requires POSIX locking"
)


async def test_custom_storage_idle_snapshot_detaches_from_live_source(responses, tmp_path):
    for output in ("first", "source-only", "restored", "reopened"):
        responses.respond(assistant_output(output))
    backend = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    storage = CustomStorage(backend)
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-checkpoint") as provider,
    ):
        agent = Agent(instructions="Respond briefly.", provider=provider)
        async with agent.session(storage=storage) as source:
            identity = source.id
            assert (
                await (await source.submit("before snapshot")).get_result()
            ).final_output == "first"
            readiness = await source.checkpoint_status(kind="idle")
            assert readiness.status == "ready"
            saved = await source.save_state()
            with pytest.raises(RuntimeError, match="already has a writer"):
                backend.acquire(identity)
            assert (
                await (await source.submit("source only")).get_result()
            ).final_output == "source-only"
            async with agent.session(state=saved) as restored:
                assert (
                    await (await restored.submit("restored only")).get_result()
                ).final_output == "restored"
        async with agent.session(storage=storage, resume=identity) as reopened:
            assert (
                await (await reopened.submit("reopened only")).get_result()
            ).final_output == "reopened"

    restored_input = json.dumps(responses.requests[2]["input"])
    reopened_input = json.dumps(responses.requests[3]["input"])
    assert "before snapshot" in restored_input and "source only" not in restored_input
    assert "source only" in reopened_input and "restored only" not in reopened_input


async def test_idle_export_refuses_to_detach_encrypted_retention(responses, tmp_path):
    backend = SQLiteSessionStore(tmp_path / "encrypted.sqlite")
    storage = CustomStorage(EncryptedSessionStore(backend, encryption_key="checkpoint-key", ttl=60))
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("continues"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-retention") as provider,
    ):
        agent = Agent(instructions="Continue the conversation.", provider=provider)
        async with agent.session(storage=storage) as source:
            assert (await (await source.submit("before")).get_result()).final_output == "first"
            assert (await source.checkpoint_status(kind="idle")).status == "not_ready"
            with pytest.raises(CapabilityError, match="Encrypted history retention"):
                await source.save_state()
            assert (await (await source.submit("after")).get_result()).final_output == "continues"
