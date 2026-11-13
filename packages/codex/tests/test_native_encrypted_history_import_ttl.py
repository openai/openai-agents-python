"""Transaction imports acquire an age without renewing retained history."""

import json
import sys

import pytest

from agents import Agent, CustomStorage, EncryptedSessionStore, NativeProvider, SQLiteSessionStore
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SQLiteSessionStore requires POSIX file locking"
)


@pytest.mark.parametrize("retained_prefix", [False, True])
async def test_encrypted_ttl_accepts_new_imported_tool_pair(responses, tmp_path, retained_prefix):
    backend = SQLiteSessionStore(tmp_path / "encrypted-import.sqlite")
    storage = CustomStorage(EncryptedSessionStore(backend, encryption_key="import-key", ttl=60))
    prefix = (
        [
            {
                "type": "message",
                "id": "msg_retained_old",
                "role": "user",
                "content": [{"type": "input_text", "text": "EXPIRED_RETAINED_PREFIX"}],
                "internal_chat_message_metadata_passthrough": {"create_time": 1},
            }
        ]
        if retained_prefix
        else []
    )
    imported = [
        {
            "type": "function_call",
            "id": "fc_ttl_imported",
            "call_id": "ttl-imported",
            "name": "historical_lookup",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "ttl-imported",
            "output": "FRESH_IMPORTED_RESULT",
        },
    ]
    responses.respond(assistant_output("continued"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-import-ttl") as provider:
        agent = Agent(instructions="Continue the imported history.", provider=provider)
        async with timeout(25), agent.session(storage=storage) as session:
            initial = await session.history_snapshot()
            await session.transact_history(expected_revision=initial.revision, keep=0, items=prefix)
            before = await session.history_snapshot()
            await session.transact_history(
                expected_revision=before.revision, keep=before.total, items=imported
            )
            after = await session.history_snapshot()
            assert after.items == tuple(prefix + imported)
            context = json.dumps((await session.model_context()).items)
            assert "FRESH_IMPORTED_RESULT" in context and "fc_ttl_imported" in context
            assert "EXPIRED_RETAINED_PREFIX" not in context
            assert (
                await (await session.submit("continue")).get_result()
            ).final_output == "continued"
    assert len(responses.requests) == 1
    wire = json.dumps(responses.requests[0]["input"])
    assert "FRESH_IMPORTED_RESULT" in wire and "fc_ttl_imported" in wire
    assert "EXPIRED_RETAINED_PREFIX" not in wire
