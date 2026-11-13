import json
import sys
from contextlib import AsyncExitStack

import pytest

from agents import Agent, CustomStorage, EncryptedSessionStore, NativeProvider, SQLiteSessionStore
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SQLiteSessionStore requires POSIX file locking"
)


@pytest.mark.parametrize("role", ["developer", "system"])
@pytest.mark.parametrize("cold", [False, True], ids=["hot", "cold"])
async def test_caller_instruction_roles_expire_without_retiring_native_instruction_receipts(
    tmp_path, responses, role, cold
):
    marker = "EXPIRED_CALLER_INSTRUCTION"
    imported = {
        "type": "message",
        "id": "msg_caller_instruction",
        "role": role,
        "content": [{"type": "input_text", "text": marker}],
        "internal_chat_message_metadata_passthrough": {"create_time": 1},
    }
    backend = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    storage = CustomStorage(
        EncryptedSessionStore(backend, encryption_key="synthetic-role-age", ttl=0)
    )
    responses.respond(assistant_output("current native answer"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-role-age", supports_websockets=False
        ) as provider,
        AsyncExitStack() as leases,
    ):
        agent = Agent(instructions="CURRENT_NATIVE_INSTRUCTION", model="gpt-5.5", provider=provider)
        source = await leases.enter_async_context(agent.session(storage=storage))
        before = await source.history_snapshot()
        await source.transact_history(expected_revision=before.revision, keep=0, items=[imported])
        identity = source.id
        if cold:
            await source.aclose()
            source = await leases.enter_async_context(
                agent.session(storage=storage, resume=identity)
            )
        projected = json.dumps((await source.model_context()).items)
        assert (
            await (await source.submit("Continue.")).get_result()
        ).final_output == "current native answer"
        wire = json.dumps(responses.requests[0])
        assert "CURRENT_NATIVE_INSTRUCTION" in wire
        assert imported in (await source.history_snapshot()).items
        assert marker not in projected
        assert marker not in wire
