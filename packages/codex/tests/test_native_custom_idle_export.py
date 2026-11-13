"""An idle canonical session can snapshot into isolated native memory."""

import json
import os

import pytest

from agents import (
    Agent,
    CustomStorage,
    EncryptedSessionStore,
    NativeProvider,
    SessionState,
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


async def test_custom_checkpoint_restores_encrypted_ttl_without_exposing_expired_items(
    responses, tmp_path
):
    expired = "EXPIRED_CHECKPOINT_ITEM"
    fresh = "FRESH_CHECKPOINT_ITEM"
    old_item = {
        "type": "message",
        "id": "msg_export_old",
        "role": "user",
        "content": [{"type": "input_text", "text": expired}],
        "internal_chat_message_metadata_passthrough": {"create_time": 1},
    }
    backend = SQLiteSessionStore(tmp_path / "encrypted.sqlite")
    storage = CustomStorage(EncryptedSessionStore(backend, encryption_key="checkpoint-key", ttl=60))
    for output in ("first", "restored"):
        responses.respond(assistant_output(output))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-retention") as provider,
    ):
        agent = Agent(instructions="Continue the conversation.", provider=provider)
        async with agent.session(storage=storage) as source:
            initial = await source.history_snapshot()
            await source.transact_history(
                expected_revision=initial.revision, keep=0, items=[old_item]
            )
            assert (await (await source.submit(fresh)).get_result()).final_output == "first"
            saved = await source.save_state()

        payload = saved.to_json()
        assert expired in payload
        parsed = SessionState.from_json(payload)
        assert parsed.agent_options["_sdk_history_ttl_seconds"] == 60
        async with agent.session(state=parsed) as restored:
            assert expired in json.dumps((await restored.history_snapshot()).items)
            retained = json.dumps((await restored.model_context()).items)
            assert expired not in retained and fresh in retained
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "restored"
            assert (await restored.save_state()).agent_options["_sdk_history_ttl_seconds"] == 60

    assert expired not in json.dumps(responses.requests[1]["input"])
    assert fresh in json.dumps(responses.requests[1]["input"])
