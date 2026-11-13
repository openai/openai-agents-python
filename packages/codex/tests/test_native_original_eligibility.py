"""Stored raw history filtering and clear use the original native identity."""

import json
import sys

import pytest

from agents import (
    Agent,
    CapabilityError,
    CustomStorage,
    LocalStorage,
    NativeProvider,
    SQLiteSessionStore,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses


def assert_only_collaboration_rows(items):
    for item in items:
        assert item["type"] == "message" and item["role"] == "developer"
        assert item["internal_chat_message_metadata_passthrough"]["content_item_kinds"] == [
            "collaboration_mode.instructions"
        ]


def assert_original_prefix(snapshot, expected):
    assert snapshot.items[: len(expected)] == tuple(expected)
    assert_only_collaboration_rows(snapshot.items[len(expected) :])


def originals():
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "KEEP_CONTEXT"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "DELETE_MIDDLE"}],
        },
        {
            "type": "function_call",
            "call_id": "old-call",
            "name": "historical",
            "arguments": '{"key":"already-done"}',
        },
        {"type": "function_call_output", "call_id": "old-call", "output": "KEEP_RECEIPT"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "KEEP_ASSISTANT"}],
        },
        {
            "type": "message",
            "id": "caller-developer",
            "role": "developer",
            "content": [{"type": "input_text", "text": "KEEP_CALLER_DEVELOPER"}],
        },
        {"type": "application_audit", "value": "AUDIT_ONLY"},
    ]


@pytest.mark.parametrize(
    "kind",
    [
        "local",
        pytest.param(
            "custom",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="SQLiteSessionStore requires POSIX file locking"
            ),
        ),
    ],
)
async def test_stored_middle_filter_pair_delete_and_clear_keep_identity(responses, tmp_path, kind):
    effects = []

    @function_tool
    def historical(key: str) -> str:
        effects.append(key)
        return "unexpected execution"

    storage = (
        LocalStorage(tmp_path / "store")
        if kind == "local"
        else CustomStorage(SQLiteSessionStore(tmp_path / "store.sqlite"))
    )
    responses.respond(assistant_output("filtered request"))
    responses.respond(assistant_output("cleared request"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-filter", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="Answer.", tools=[historical], provider=provider)
        retained = [item for index, item in enumerate(originals()) if index != 1]
        async with agent.session(storage=storage) as session:
            identity = session.id
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=originals()
            )
            before = await session.history_snapshot()
            await session.transact_history(
                expected_revision=before.revision, keep=0, items=retained
            )
            assert session.id == identity
            assert (await session.history_snapshot()).items == tuple(retained)
            if kind == "custom":
                with pytest.raises(
                    CapabilityError, match="Stored forks require native LocalStorage"
                ):
                    await session.fork_stored()
            assert effects == [] and responses.requests == []
        async with agent.session(storage=storage, resume=identity) as session:
            assert session.id == identity
            assert_original_prefix(await session.history_snapshot(), retained)
            assert (
                await (await session.submit("Continue filtered.")).get_result()
            ).final_output == "filtered request"
            wire = json.dumps(responses.requests[0]["input"])
            assert "DELETE_MIDDLE" not in wire and "AUDIT_ONLY" not in wire
            assert "KEEP_CONTEXT" in wire and "KEEP_RECEIPT" in wire
            assert "KEEP_CALLER_DEVELOPER" in wire
            current = await session.history_snapshot()
            without_pair = [item for item in current.items if item.get("call_id") != "old-call"]
            await session.transact_history(
                expected_revision=current.revision, keep=0, items=without_pair
            )
            assert (await session.history_snapshot()).items == tuple(without_pair)
            assert "KEEP_RECEIPT" not in json.dumps((await session.model_context()).items)
            filtered = await session.history_snapshot()
            await session.transact_history(expected_revision=filtered.revision, keep=0)
            assert (await session.history_snapshot()).items == ()
        async with agent.session(storage=storage, resume=identity) as session:
            assert session.id == identity
            assert_only_collaboration_rows((await session.history_snapshot()).items)
            assert (
                await (await session.submit("Fresh input.")).get_result()
            ).final_output == "cleared request"
            wire = json.dumps(responses.requests[1]["input"])
            assert "KEEP_" not in wire and "DELETE_MIDDLE" not in wire and "AUDIT_ONLY" not in wire
            assert effects == []


async def test_clearing_stored_branch_keeps_parent_materialized_history(responses, tmp_path):
    storage = LocalStorage(tmp_path / "branches")
    responses.respond(assistant_output("parent retained"))
    responses.respond(assistant_output("branch fresh"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-branch-clear", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="Answer.", provider=provider)
        values = [originals()[0], originals()[4], originals()[5]]
        async with agent.session(storage=storage) as source:
            parent_id = source.id
            initial = await source.history_snapshot()
            await source.transact_history(expected_revision=initial.revision, keep=0, items=values)
            branch_id = await source.fork_stored()
            assert branch_id != parent_id
        async with agent.session(storage=storage, resume=branch_id) as branch:
            current = await branch.history_snapshot()
            assert_original_prefix(current, values)
            await branch.transact_history(expected_revision=current.revision, keep=0)
            assert (await branch.history_snapshot()).items == ()
        async with agent.session(storage=storage, resume=parent_id) as parent:
            assert_original_prefix(await parent.history_snapshot(), values)
            assert (
                await (await parent.submit("Continue parent.")).get_result()
            ).final_output == "parent retained"
            assert "KEEP_CONTEXT" in json.dumps(responses.requests[0]["input"])
        async with agent.session(storage=storage, resume=branch_id) as branch:
            assert_only_collaboration_rows((await branch.history_snapshot()).items)
            assert (
                await (await branch.submit("New branch input.")).get_result()
            ).final_output == "branch fresh"
            assert "KEEP_" not in json.dumps(responses.requests[1]["input"])


@pytest.mark.parametrize(
    "kind",
    [
        "local",
        pytest.param(
            "custom",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="SQLiteSessionStore requires POSIX file locking"
            ),
        ),
    ],
)
async def test_bounded_originals_exclude_standalone_native_instruction_receipts(
    responses, tmp_path, kind
):
    storage = (
        LocalStorage(tmp_path / "store")
        if kind == "local"
        else CustomStorage(SQLiteSessionStore(tmp_path / "store.sqlite"))
    )
    items = [{"type": "future_history_record", "large": "x" * (128 * 1024)}, *originals()[-2:]]
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-originals", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="Opening configuration.", provider=provider)
        async with agent.session(storage=storage) as session:
            initial = await session.history_snapshot()
            await session.transact_history(expected_revision=initial.revision, keep=0, items=items)
            before = await session.history_snapshot()
            assert before.items == tuple(items)
            await session.update(instructions="Current native configuration.")
            current = await session.history_snapshot()
            assert current.items[: before.total] == before.items
            for row in current.items[before.total :]:
                assert row["type"] == "message" and row["role"] == "developer"
                kinds = row["internal_chat_message_metadata_passthrough"]["content_item_kinds"]
                assert "collaboration_mode.instructions" in kinds
                assert set(kinds) <= {
                    "collaboration_mode.instructions",
                    "sdk_application.instructions",
                }
            bounded = await session.history_snapshot(limit=len(items))
            assert bounded.revision == current.revision and bounded.total == current.total
            assert bounded.items == current.items[-len(items) :]
            context = json.dumps((await session.model_context()).items)
            assert "Current native configuration." in context
            assert "KEEP_CALLER_DEVELOPER" in context
            assert not responses.requests
