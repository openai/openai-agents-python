"""Raw import ages can shorten, but cannot extend, native encrypted retention."""

import asyncio
import json
import sys
import time
from contextlib import AsyncExitStack

import pytest

from agents import Agent, CustomStorage, EncryptedSessionStore, NativeProvider, SQLiteSessionStore
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SQLiteSessionStore requires POSIX file locking"
)


def envelopes(value, identity):
    if isinstance(value, dict):
        if isinstance(value.get("item"), dict) and value["item"].get("id") == identity:
            yield value
        for child in value.values():
            yield from envelopes(child, identity)
    elif isinstance(value, list):
        for child in value:
            yield from envelopes(child, identity)


@pytest.mark.parametrize("cold", [False, True], ids=["hot", "cold"])
async def test_future_raw_age_cannot_extend_ttl_or_refresh_retained_prefix(
    responses, tmp_path, cold
):
    documents = []

    class ObservedEncryptedStore(EncryptedSessionStore):
        def commit(self, session_id, token, expected_revision, write_id, payload):
            super().commit(session_id, token, expected_revision, write_id, payload)
            documents.append(json.loads(payload))

    backend = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    encrypted = ObservedEncryptedStore(backend, encryption_key="synthetic-future-age", ttl=0)
    storage = CustomStorage(encrypted)
    old = {
        "type": "message",
        "id": "msg_old",
        "role": "user",
        "content": [{"type": "input_text", "text": "EXPIRED_OLD_IMPORT"}],
        "internal_chat_message_metadata_passthrough": {"create_time": 1},
    }
    future = {
        "type": "message",
        "id": "msg_future",
        "role": "user",
        "content": [{"type": "input_text", "text": "EXPIRED_FUTURE_IMPORT"}],
        "internal_chat_message_metadata_passthrough": {"create_time": 2**64 - 1},
    }
    responses.respond(assistant_output("continued"))
    async with (
        timeout(25),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-future-age", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=provider)
        async with agent.session(storage=storage) as source:
            identity = source.id
            before = await source.history_snapshot()
            await source.transact_history(expected_revision=before.revision, keep=0, items=[old])
            before = await source.history_snapshot()
            await source.transact_history(
                expected_revision=before.revision, keep=before.total, items=[future]
            )
            accepted_second = int(time.time())
            admitted_document = documents[-1]
            # TTL zero retains only its admission second. Wait for the observed
            # boundary instead of assuming a fixed sleep crosses that boundary.
            while int(time.time()) <= accepted_second:
                await asyncio.sleep(0.01)
            if cold:
                await source.aclose()
            async with AsyncExitStack() as scope:
                active = (
                    await scope.enter_async_context(agent.session(storage=storage, resume=identity))
                    if cold
                    else source
                )
                context = json.dumps((await active.model_context()).items)
                audit = (await active.history_snapshot()).items
                assert old in audit and future in audit
                result = await (await active.submit("Continue now.")).get_result()
                assert result.final_output == "continued"
    assert len(responses.requests) == 1
    wire = json.dumps(responses.requests[0]["input"])
    assert "EXPIRED_OLD_IMPORT" not in context and "EXPIRED_OLD_IMPORT" not in wire
    assert "EXPIRED_FUTURE_IMPORT" not in context and "EXPIRED_FUTURE_IMPORT" not in wire
    old_envelopes = list(envelopes(admitted_document, "msg_old"))
    future_envelopes = list(envelopes(admitted_document, "msg_future"))
    assert old_envelopes and future_envelopes
    assert all(row["metadata"]["sdk_created_at"] == 1 for row in old_envelopes)
    assert all(
        0 <= row["metadata"]["sdk_created_at"] <= accepted_second + 1 for row in future_envelopes
    )


@pytest.mark.parametrize("future", [False, True], ids=["old-age", "unbounded-future"])
async def test_enabling_ttl_does_not_trust_a_previously_imported_future_age(
    responses, tmp_path, future
):
    database = tmp_path / "enable-ttl.sqlite"
    item = {
        "type": "message",
        "id": "msg_preexisting",
        "role": "user",
        "content": [{"type": "input_text", "text": "PREEXISTING_IMPORT"}],
        "internal_chat_message_metadata_passthrough": {"create_time": 2**64 - 1 if future else 1},
    }
    async with (
        timeout(25),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-enable-ttl", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=provider)
        async with agent.session(
            storage=CustomStorage(
                EncryptedSessionStore(
                    SQLiteSessionStore(database), encryption_key="synthetic-upgrade"
                )
            )
        ) as source:
            identity = source.id
            before = await source.history_snapshot()
            await source.transact_history(expected_revision=before.revision, keep=0, items=[item])
        storage = CustomStorage(
            EncryptedSessionStore(
                SQLiteSessionStore(database), encryption_key="synthetic-upgrade", ttl=0
            )
        )
        if future:
            with pytest.raises(
                RuntimeError, match="future creation time without SDK admission metadata"
            ):
                async with agent.session(storage=storage, resume=identity) as restored:
                    await restored.model_context()
        else:
            async with agent.session(storage=storage, resume=identity) as restored:
                assert "PREEXISTING_IMPORT" not in json.dumps(
                    (await restored.model_context()).items
                )
                assert item in (await restored.history_snapshot()).items
    assert responses.requests == []
