"""Canonical history transactions survive native restore and drive the next request."""

import json
import os

import pytest

from agents import (
    Agent,
    CustomStorage,
    NativeError,
    NativeProvider,
    SQLiteSessionStore,
    function_tool,
)
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.native_storage_fixtures import MemorySessionStore


async def test_provider_item_references_survive_native_history_restore(responses):
    references = [
        {"type": "item_reference", "id": "msg_remote_first"},
        {"type": "item_reference", "id": "msg_remote_second"},
        {"type": "item_reference", "id": "msg_remote_first"},
    ]
    storage = CustomStorage(MemorySessionStore())
    responses.respond(assistant_output("continued"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-history") as provider:
        agent = Agent(instructions="Answer briefly.", provider=provider)
        async with agent.session(storage=storage) as session:
            session_id = session.id
            snapshot = await session.history_snapshot()
            for invalid in [
                {"type": "item_reference"},
                {"type": "item_reference", "id": 17},
            ]:
                with pytest.raises(RuntimeError, match="Invalid canonical history item"):
                    await session.transact_history(
                        expected_revision=snapshot.revision, keep=0, items=[invalid]
                    )
                assert (await session.history_snapshot()).revision == snapshot.revision
            await session.transact_history(
                expected_revision=snapshot.revision, keep=0, items=references
            )
            assert (await session.history_snapshot()).items == tuple(references)
        async with agent.session(storage=storage, resume=session_id) as restored:
            restored_items = (await restored.history_snapshot()).items
            assert [
                item for item in restored_items if item["type"] == "item_reference"
            ] == references
            assert responses.requests == []
            result = await (await restored.submit("continue")).get_result()
    assert result.final_output == "continued"
    assert len(responses.requests) == 1
    wire = responses.requests[0]["input"]
    assert [item for item in wire if item["type"] == "item_reference"] == references


async def test_replay_capture_rejects_oversized_original_tool_output(responses):
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("completed")
        return "x" * (8 * 1024 * 1024 + 1)

    responses.respond(function_call("lookup", {}, "oversized-original"))
    responses.respond(assistant_output("large tool result settled"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-history-budget"
    ) as provider:
        agent = Agent(instructions="Use lookup.", provider=provider, tools=[lookup])
        with pytest.raises(NativeError, match="completed.*history exceeds the result size limit"):
            await agent.run("read the document", return_history=True)
    assert effects == ["completed"]
    assert len(responses.requests) == 2


async def test_replay_capture_ignores_discarded_oversized_tool_output(responses):
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("completed")
        return "x" * (8 * 1024 * 1024 + 1)

    responses.respond(function_call("lookup", {}, "discarded-original"))
    responses.respond(assistant_output("large tool result settled"))
    responses.respond(assistant_output("small history continued"))
    retained = {"type": "application_note", "value": "kept" * (20 * 1024)}
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-history-budget"
    ) as provider:
        agent = Agent(instructions="Use lookup.", provider=provider, tools=[lookup])
        async with agent.session() as session:
            assert (
                await (await session.submit("read the document")).get_result()
            ).final_output == ("large tool result settled")
            before = await session.history_snapshot(limit=0)
            await session.transact_history(
                expected_revision=before.revision, keep=0, items=[retained]
            )
            state = await session.save_state()
        result = await agent.run("continue", state=state, return_history=True)
        assert result.final_output == "small history continued"
        assert result.history is not None
        assert result.history[0] == retained
        assert all(item.get("call_id") != "discarded-original" for item in result.history)
    assert effects == ["completed"]
    assert len(responses.requests) == 3


@pytest.mark.parametrize("return_state", [False, True])
async def test_one_shot_history_can_continue_in_another_session(responses, return_state):
    calls = []

    @function_tool
    def lookup(key: str) -> str:
        calls.append(key)
        return f"record:{key}"

    responses.respond(function_call("lookup", {"key": "first"}, "detached-lookup"))
    responses.respond(assistant_output("record loaded"))
    responses.respond(assistant_output("continued with the record"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-history") as provider:
        producer = Agent(instructions="Look up the record.", tools=[lookup], provider=provider)
        result = await producer.run(
            "load my record",
            initial_messages=[{"role": "user", "content": "my prior request"}],
            return_history=True,
            return_state=return_state,
        )
        assert result.final_output == "record loaded"
        assert (result.state is not None) is return_state
        assert result.history is not None

        consumer = Agent(instructions="Use the previous record.", provider=provider)
        async with consumer.session() as session:
            before = await session.history_snapshot()
            await session.transact_history(
                expected_revision=before.revision, keep=0, items=result.history
            )
            continued = await (await session.submit("continue")).get_result()
            assert continued.final_output == "continued with the record"
            assert continued.history is None

    assert calls == ["first"]
    assert len(responses.requests) == 3
    items = responses.requests[-1]["input"]
    replayed = [item for item in items if item.get("call_id") == "detached-lookup"]
    assert [item["type"] for item in replayed] == ["function_call", "function_call_output"]
    assert "record:first" in json.dumps(replayed[-1])
    messages = json.dumps([item for item in items if item["type"] == "message"])
    assert "Look up the record." not in messages
    assert "Use the previous record." in messages
    assert messages.index("my prior request") < messages.index("load my record")
    assert (
        messages.index("load my record")
        < messages.index("record loaded")
        < messages.index("continue")
    )


@pytest.mark.parametrize("declare_approval_tool", [False, True])
async def test_one_shot_text_history_is_opt_in(responses, declare_approval_tool):
    @function_tool(needs_approval=True)
    def lookup(key: str) -> str:
        raise AssertionError(f"Unexpected call for {key}")

    captured_output = "captured output" + "x" * (128 * 1024)
    responses.respond(assistant_output("ordinary output"))
    responses.respond(assistant_output(captured_output))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-history") as provider:
        agent = Agent(
            instructions="Answer.",
            provider=provider,
            tools=[lookup] if declare_approval_tool else (),
        )
        ordinary = await agent.run("ordinary input")
        captured = await agent.run("captured input", return_history=True)
    assert ordinary.history is None
    assert captured.history is not None
    assert "captured input" in json.dumps(captured.history)
    assert captured_output in json.dumps(captured.history)


async def test_one_shot_rejects_an_oversized_history_after_completion(responses):
    responses.respond(assistant_output("completed"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-history") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        initial = [{"role": "user", "content": f"Earlier turn {index}"} for index in range(4097)]
        with pytest.raises(NativeError, match="completed.*history exceeds the result size limit"):
            await agent.run("finish", initial_messages=initial, return_history=True)
    assert len(responses.requests) == 1


async def test_committed_history_flush_failure_retires_live_session(responses):
    class FailCheckpointFlush(MemorySessionStore):
        armed = False
        accepted_checkpoint = False

        def commit(self, session_id, token, expected_revision, write_id, payload):
            super().commit(session_id, token, expected_revision, write_id, payload)
            if self.armed and any(
                item.get("type") == "compacted" and "committed replacement" in json.dumps(item)
                for item in json.loads(payload)["items"]
            ):
                self.accepted_checkpoint = True

        def flush(self, session_id, token):
            if self.armed and self.accepted_checkpoint:
                self.armed = False
                raise OSError("synthetic flush failure after committed checkpoint")
            return super().flush(session_id, token)

    responses.respond(assistant_output("restored replacement"))
    backend = FailCheckpointFlush()
    storage = CustomStorage(backend)
    async with NativeProvider(base_url=responses.url, api_key="synthetic-uncertain") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(
            storage=storage, initial_messages=[{"role": "user", "content": "original history"}]
        ) as session:
            identity = session.id
            before = await session.history_snapshot()
            backend.armed = True
            with pytest.raises(RuntimeError, match="flush|persistence|commit"):
                await session.transact_history(
                    expected_revision=before.revision,
                    keep=0,
                    items=[
                        {
                            "type": "message",
                            "id": "replacement",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "committed replacement"}],
                        }
                    ],
                )
            assert backend.accepted_checkpoint
            try:
                stale = await session.history_snapshot()
            except RuntimeError:
                stale = None
            if stale is None:
                with pytest.raises(RuntimeError):
                    await session.submit("must not execute")
            assert responses.requests == []
        async with agent.session(storage=storage, resume=identity) as restored:
            current = await restored.history_snapshot()
            assert "committed replacement" in json.dumps(current.items)
            assert "original history" not in json.dumps(current.items)
            assert current.revision != before.revision
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "restored replacement"
        assert stale is None, "durable replacement failed but the old live history remained usable"


async def test_atomic_canonical_history_replacement_and_restore(responses):
    responses.respond(assistant_output("continued"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-history-key")
    agent = Agent(instructions="Answer briefly.", provider=provider)
    async with agent.session(
        initial_messages=[{"role": "user", "content": "old input"}]
    ) as session:
        before = await session.history_snapshot()
        assert before.total == len(before.items)
        tail = await session.history_snapshot(limit=1)
        assert tail.revision == before.revision and tail.items == before.items[-1:]
        empty = await session.history_snapshot(limit=0)
        assert empty.items == () and empty.total == before.total
        replacement = [
            {
                "type": "function_call",
                "id": "fc-history",
                "call_id": "call-history",
                "name": "lookup",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "id": "out-history",
                "call_id": "call-history",
                "output": "imported result",
            },
        ]
        revision = await session.transact_history(
            expected_revision=before.revision, keep=0, items=replacement
        )
        current = await session.history_snapshot()
        assert current.revision == revision and current.total == 2
        assert [item["type"] for item in current.items] == ["function_call", "function_call_output"]
        with pytest.raises(RuntimeError, match="revision conflict"):
            await session.transact_history(expected_revision=before.revision, keep=0)
        assert (await session.history_snapshot()).revision == revision
        with pytest.raises(RuntimeError, match="Invalid canonical history item"):
            await session.transact_history(
                expected_revision=revision,
                keep=0,
                items=[{"type": "function_call", "arguments": 17}],
            )
        assert (await session.history_snapshot()).revision == revision
        state = await session.save_state()
    async with agent.session(state=state) as restored:
        resumed = await restored.history_snapshot()
        assert resumed.items == current.items
        assert resumed.revision == revision
        assert (await (await restored.submit("continue")).get_result()).final_output == "continued"
    request = json.dumps(responses.requests[-1]["input"])
    assert "old input" not in request
    assert "imported result" in request and "call-history" in request


@pytest.mark.parametrize("family", ["function", "custom", "search", "computer"])
async def test_history_transactions_reject_split_or_unpaired_client_tools(responses, family):
    responses.respond(assistant_output("pairs preserved"))
    call = {"id": "call-item", "call_id": "history-pair"}
    output = {"id": "result-item", "call_id": "history-pair"}
    if family == "function":
        call.update(type="function_call", name="lookup", arguments="{}")
        output.update(type="function_call_output", output="retained result")
    elif family == "custom":
        call.update(type="custom_tool_call", name="lookup", input="raw input")
        output.update(type="custom_tool_call_output", output="retained result")
    elif family == "computer":
        call.update(type="computer_call", actions=[{"type": "wait"}], pending_safety_checks=[])
        output.update(
            type="computer_call_output",
            output={"type": "computer_screenshot", "image_url": "data:image/png;base64,AA=="},
            acknowledged_safety_checks=[],
        )
    else:
        call.update(type="tool_search_call", execution="client", arguments={})
        output.update(type="tool_search_output", execution="client", status="completed", tools=[])
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-history-pairs"
    ) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=[call, output]
            )
            retained = await session.history_snapshot()
            for keep, appended in [
                (1, []),
                (1, [output]),
                (0, [call]),
                (0, [output]),
                (0, [output, call]),
            ]:
                with pytest.raises(RuntimeError, match="complete tool call/result pairs"):
                    await session.transact_history(
                        expected_revision=retained.revision, keep=keep, items=appended
                    )
                assert await session.history_snapshot() == retained
                assert responses.requests == []
            assert (await (await session.submit("continue")).get_result()).final_output == (
                "pairs preserved"
            )
    wire = responses.requests[-1]["input"]
    assert sum(item.get("call_id") == "history-pair" for item in wire) == 2
    assert not any(item.get("output") == "aborted" for item in wire)


async def test_history_transactions_preserve_self_contained_provider_hosted_records(responses):
    responses.respond(assistant_output("hosted preserved"))
    items = [
        {
            "type": "web_search_call",
            "id": "hosted-search",
            "status": "completed",
            "action": {"type": "search", "query": "synthetic query"},
        },
        {
            "type": "tool_search_output",
            "id": "server-search-output",
            "call_id": None,
            "execution": "server",
            "status": "completed",
            "tools": [],
        },
    ]
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-history"
    ) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            before = await session.history_snapshot()
            await session.transact_history(expected_revision=before.revision, keep=0, items=items)
            retained = await session.history_snapshot()
            assert [item["type"] for item in retained.items] == [
                "web_search_call",
                "tool_search_output",
            ]
            assert (await (await session.submit("continue")).get_result()).final_output == (
                "hosted preserved"
            )
    wire = responses.requests[-1]["input"]
    assert any(item.get("type") == "web_search_call" for item in wire)
    assert any(
        item.get("type") == "tool_search_output" and item.get("execution") == "server"
        for item in wire
    )


async def test_original_values_survive_restore_without_becoming_model_work(responses):
    responses.respond(assistant_output("originals preserved"))
    originals = [
        {
            "type": "future_tool_call",
            "name": "do_not_run",
            "call_id": "audit-call",
            "payload": {"large": 10**80, "nested": [None, True, {"extra": "retained"}]},
        },
        {"type": 17, "untyped": "audit-only"},
        {"type": "future", "nested": {"$serde_json::private::Number": "7"}},
        {"type": "future", "nested": {"$serde_json::private::RawValue": '{"converted":true}'}},
        {"$serde_json::private::Number": "7"},
        {
            "$serde_json::private::RawValue": json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "must remain audit only"}],
                }
            )
        },
        {"type": {"$serde_json::private::RawValue": '"message"'}},
        {"__proto__": {"unchanged": True}},
        {"type": "configuration_update", "opaque": "audit-only"},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "known message"}],
            "unknown_field": "original-only",
        },
    ]
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-original-values"
    ) as provider:
        agent = Agent(instructions="Answer briefly.", provider=provider)
        async with agent.session() as session:
            before = await session.history_snapshot()
            revision = await session.transact_history(
                expected_revision=before.revision, keep=0, items=originals
            )
            current = await session.history_snapshot()
            assert current.items == tuple(originals)
            assert current.total == len(originals) and current.revision == revision
            assert "id" not in current.items[-1]
            tail = await session.history_snapshot(limit=1)
            assert tail.items == (originals[-1],) and tail.total == len(originals)
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert await restored.history_snapshot() == current
            result = await (await restored.submit("continue")).get_result()
            assert result.final_output == "originals preserved"
    wire = json.dumps(responses.requests[-1]["input"])
    assert "known message" in wire
    assert "audit-call" not in wire and "audit-only" not in wire
    assert "original-only" not in wire and "do_not_run" not in wire
    assert "must remain audit only" not in wire and "converted" not in wire


async def test_audit_rows_count_in_keep_but_cannot_complete_an_executable_pair(responses):
    call = {
        "type": "function_call",
        "call_id": "original-pair",
        "name": "lookup",
        "arguments": "{}",
    }
    output = {"type": "function_call_output", "call_id": "original-pair", "output": "retained"}
    audit = {
        "type": "future_output",
        "id": "audit-data",
        "call_id": "original-pair",
        "output": "never executable",
    }
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-original-pairs"
    ) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            initial = await session.history_snapshot()
            with pytest.raises(RuntimeError, match="complete tool call/result pairs"):
                await session.transact_history(
                    expected_revision=initial.revision, keep=0, items=[call, audit]
                )
            assert await session.history_snapshot() == initial
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=[audit, call, output, audit]
            )
            complete = await session.history_snapshot()
            assert complete.total == 4
            with pytest.raises(RuntimeError, match="complete tool call/result pairs"):
                await session.transact_history(expected_revision=complete.revision, keep=2)
            assert await session.history_snapshot() == complete
            await session.transact_history(expected_revision=complete.revision, keep=1)
            assert (await session.history_snapshot()).items == (audit,)
            assert responses.requests == []


async def test_original_catalog_survives_supplied_compaction_and_rejects_ambiguous_edits(responses):
    responses.respond(assistant_output("summary continued"))
    responses.respond(assistant_output("replacement continued"))
    originals = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old original first"}],
            "extra": 17,
        },
        {"type": "application_note", "value": "audit between messages"},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old original second"}],
        },
    ]
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-original-compaction"
    ) as provider:
        agent = Agent(instructions="Answer briefly.", provider=provider)
        async with agent.session() as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=originals
            )
            before = await session.history_snapshot()
            await session.compact(summary="application summary only")
            assert await session.history_snapshot() == before
            model = await session.model_context()
            assert "application summary only" in json.dumps(model.items)
            assert "old original" not in json.dumps(model.items)
            with pytest.raises(
                RuntimeError, match="cannot shorten original history across model compaction"
            ):
                await session.transact_history(expected_revision=before.revision, keep=1)
            assert await session.history_snapshot() == before
            assert (await session.model_context()).items == model.items
            assert responses.requests == []
            audit = {"type": "application_note", "value": "after compaction"}
            await session.transact_history(
                expected_revision=before.revision, keep=before.total, items=[audit]
            )
            appended = await session.history_snapshot()
            assert appended.items == (*before.items, audit)
            assert (await session.model_context()).items == model.items
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert await restored.history_snapshot() == appended
            assert (
                await (await restored.submit("after append")).get_result()
            ).final_output == "summary continued"
            wire = json.dumps(responses.requests[-1]["input"])
            assert "application summary only" in wire and "old original" not in wire
            assert "audit between messages" not in wire and "after compaction" not in wire
            current = await restored.history_snapshot()
            replacement = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "explicit replacement"}],
            }
            await restored.transact_history(
                expected_revision=current.revision, keep=0, items=[replacement]
            )
            assert (await restored.history_snapshot()).items == (replacement,)
            assert (
                await (await restored.submit("after replacement")).get_result()
            ).final_output == "replacement continued"
            wire = json.dumps(responses.requests[-1]["input"])
            assert "explicit replacement" in wire and "application summary only" not in wire
            assert "old original" not in wire


async def test_native_original_tool_result_survives_prefix_edit_without_untruncating_model(
    responses,
):
    full = "original native tool output " * 10_000

    @function_tool
    def lookup() -> str:
        return full

    responses.respond(function_call("lookup", {}, "large-original-output"))
    responses.respond(assistant_output("large result settled"))
    responses.respond(assistant_output("truncated continuation"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-original-truncation"
    ) as provider:
        agent = Agent(instructions="Use lookup.", provider=provider, tools=[lookup])
        async with agent.session() as session:
            await (await session.submit("read the large result")).get_result()
            before = await session.history_snapshot()
            index = next(
                index
                for index, item in enumerate(before.items)
                if item.get("type") == "function_call_output"
                and item.get("call_id") == "large-original-output"
            )
            assert full in json.dumps(before.items[index])
            model = await session.model_context()
            projected = next(
                item
                for item in model.items
                if item.get("type") == "function_call_output"
                and item.get("call_id") == "large-original-output"
            )
            assert len(json.dumps(projected)) < len(full)
            audit = {"type": "application_note", "value": "retained prefix"}
            await session.transact_history(
                expected_revision=before.revision, keep=index + 1, items=[audit]
            )
            retained = await session.history_snapshot()
            assert retained.items == (*before.items[: index + 1], audit)
            assert projected in (await session.model_context()).items
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert await restored.history_snapshot() == retained
            assert projected in (await restored.model_context()).items
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "truncated continuation"
            wire_output = next(
                item
                for item in responses.requests[-1]["input"]
                if item.get("type") == "function_call_output"
                and item.get("call_id") == "large-original-output"
            )
            assert wire_output["output"] == projected["output"]
            assert full not in json.dumps(responses.requests[-1]["input"])


async def test_raw_original_objects_survive_custom_storage_reopen(responses, tmp_path):
    originals = [
        {"$serde_json::private::RawValue": '{"type":"function_call","name":"never"}'},
        {"type": "future", "nested": {"$serde_json::private::Number": "7"}, "large": 10**80},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "stored known input"}],
            "missing_id": True,
        },
    ]
    backend = (
        SQLiteSessionStore(tmp_path / "raw-originals.sqlite")
        if os.name == "posix"
        else MemorySessionStore()
    )
    storage = CustomStorage(backend)
    async with NativeProvider(base_url=responses.url, api_key="synthetic-raw-storage") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=originals
            )
            retained = await session.history_snapshot()
            assert retained.items == tuple(originals)
            identity = session.id
        async with agent.session(storage=storage, resume=identity) as restored:
            current = await restored.history_snapshot()
            assert current.items[: retained.total] == retained.items
            assert current.total == len(current.items)
            for item in current.items[retained.total :]:
                assert item["type"] == "message"
                assert item["role"] == "developer"
                kinds = item["internal_chat_message_metadata_passthrough"]["content_item_kinds"]
                assert kinds
                assert set(kinds) <= {
                    "collaboration_mode.instructions",
                    "sdk_application.accepted",
                }
            assert await restored.history_snapshot(limit=1) == type(current)(
                current.revision, current.total, current.items[-1:]
            )
            assert responses.requests == []


@pytest.mark.parametrize("async_value", [None, True, False], ids=["old-record", "true", "false"])
async def test_function_call_fields_survive_native_acceptance_and_replay(
    responses, async_value: bool | None
) -> None:
    calls = []

    @function_tool
    def lookup(key: str) -> str:
        calls.append(key)
        return f"value:{key}"

    event = function_call("lookup", {"key": "replay"}, "wire-call")
    event["item"]["created_by"] = "provider-owned"
    expected = {} if async_value is None else {"async": async_value, "status": "completed"}
    event["item"].update(expected)
    responses.respond(event)
    responses.respond(assistant_output("tool result accepted"))
    responses.respond(assistant_output("replayed"))

    async with NativeProvider(base_url=responses.url, api_key="synthetic-wire-alias") as provider:
        agent = Agent(instructions="Use the lookup result.", tools=[lookup], provider=provider)
        async with agent.session() as source:
            await (await source.submit("call the tool")).get_result()
            snapshot = await source.history_snapshot()
        async with agent.session() as target:
            initial = await target.history_snapshot()
            await target.transact_history(
                expected_revision=initial.revision, keep=0, items=snapshot.items
            )
            await (await target.submit("continue from the replay")).get_result()

    assert calls == ["replay"]
    assert len(responses.requests) == 3
    for items in (snapshot.items, responses.requests[-1]["input"]):
        item = next(
            item
            for item in items
            if item["type"] == "function_call" and item.get("call_id") == "wire-call"
        )
        assert {key: item[key] for key in ("async", "status") if key in item} == expected
        assert "created_by" not in item
