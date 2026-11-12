from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    NativeError,
    ResponsesCompaction,
    SessionState,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


def window(*, opaque: bool = False) -> dict[str, Any]:
    return {
        "id": "COMPACT_RESULT_NOT_AN_ANCHOR",
        "output": [
            assistant_output("RETAINED_BEFORE_OPAQUE")["item"],
            {"type": "compaction", "encrypted_content": "OPAQUE_COMPLETE_WINDOW"}
            if opaque
            else assistant_output("RETAINED_SUMMARY_BLOCK")["item"],
            {
                "type": "function_call",
                "call_id": "historic",
                "name": "historical",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "historic",
                "output": "HISTORIC_TOOL_OUTPUT",
            },
            assistant_output("RETAINED_AFTER_OPAQUE")["item"],
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }


def client_for(callback):
    return cast(AsyncOpenAI, SimpleNamespace(responses=SimpleNamespace(compact=callback)))


@pytest.mark.parametrize("profiled", [False, True])
async def test_complete_window_cold_restore_keeps_current_instructions_and_original_audit(
    responses,
    profiled,
):
    calls = []
    decisions = []

    async def compact(**body):
        calls.append(body)
        return window(opaque=True)

    def skip(context):
        decisions.append(context)
        return False

    responses.respond(assistant_output("OLD_ASSISTANT"))
    responses.respond(assistant_output("continued"))
    from tests.test_native_compaction_lineage import _matching_compaction_client

    async with _matching_compaction_client(responses, compact) as client:
        config = ResponsesCompaction(
            client=client, model="separate-compactor", mode="input", should_compact=skip
        )
        current_provider = provider(responses)
        target = Agent(
            name="Unused", instructions="Unused.", provider=current_provider, compaction=config
        )
        agent = Agent(
            instructions="ORIGINAL_APPLICATION",
            provider=current_provider,
            compaction=config,
            handoffs=[target] if profiled else (),
        )
        async with timeout(30):
            async with agent.session() as session:
                await (await session.submit("OLD_USER_SENTINEL")).get_result()
                await session.update(instructions="CURRENT_APPLICATION")
                count = len(decisions)
                await session.compact()
                assert len(decisions) == count
                saved = await session.save_state()
                assert "OLD_USER_SENTINEL" in saved.to_json()
            current_agent = (
                agent
                if profiled
                else Agent(
                    instructions="CURRENT_APPLICATION", provider=current_provider, compaction=config
                )
            )
            async with current_agent.session(
                state=SessionState.from_json(saved.to_json())
            ) as restored:
                await (await restored.submit("followup")).get_result()
        assert len(calls) == 1 and calls[0]["model"] == "separate-compactor"
        assert "OLD_USER_SENTINEL" in json.dumps(calls[0]["input"])
        body = json.dumps(responses.requests[-1])
        assert "OLD_USER_SENTINEL" not in body and "OLD_ASSISTANT" not in body
        assert "CURRENT_APPLICATION" in body and "HISTORIC_TOOL_OUTPUT" in body
        assert (
            body.index("RETAINED_BEFORE_OPAQUE")
            < body.index("OPAQUE_COMPLETE_WINDOW")
            < body.index("RETAINED_AFTER_OPAQUE")
        )
        assert (
            responses.requests[-1].get("previous_response_id") != "COMPACT_RESULT_NOT_AN_AN_ANCHOR"
        )


async def test_default_post_persistence_predicate_skips_nine_and_admits_ten(responses):
    calls = []

    async def compact(**body):
        calls.append(body)
        return window()

    responses.respond(*(assistant_output(f"candidate-{n}") for n in range(9)))
    responses.respond(assistant_output("candidate-ten"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        compaction=ResponsesCompaction(client=client_for(compact), model="compact"),
    )
    async with timeout(30), agent.session() as session:
        await (await session.submit("first")).get_result()
        assert calls == []
        await (await session.submit("second")).get_result()
        assert len(calls) == 1
        assert any(row["type"] == "compacted" for row in (await session.save_state()).history)
    assert len(responses.requests) == 2


async def test_terminal_tool_success_schedules_compaction_before_terminal_retirement(responses):
    calls = []
    effects = []

    async def compact(**body):
        calls.append(body)
        return window()

    @function_tool(finish_turn=True)
    def finish() -> str:
        effects.append("once")
        return "terminal result"

    responses.respond(function_call("finish", {}, "finish-call"))
    config = ResponsesCompaction(
        client=client_for(compact), model="compact", should_compact=lambda context: True
    )
    agent = Agent(
        instructions="Use finish.", provider=provider(responses), tools=[finish], compaction=config
    )
    async with timeout(30), agent.session() as session:
        result = await (await session.submit("finish now")).get_result()
        assert result.final_output == "terminal result"
        assert effects == ["once"] and len(calls) == 1 and len(responses.requests) == 1
        assert "finish-call" in (await session.save_state()).to_json()


@pytest.mark.parametrize("failure", ["predicate", "client", "missing_anchor", "malformed_id"])
async def test_optional_failure_keeps_completed_answer_and_original_context(responses, failure):
    calls = []
    seen = []

    async def compact(**body):
        calls.append(body)
        if failure == "malformed_id":
            return {**window(), "id": 7}
        raise RuntimeError("compact unavailable")

    async def predicate(context):
        seen.append(context)
        assert context.mode == "input" and context.model == "compact"
        assert all(item.get("role") != "user" for item in context.candidates)
        if failure == "predicate":
            raise RuntimeError("predicate unavailable")
        if failure == "client":
            context.items[0]["content"] = [
                {"type": "input_text", "text": "MUTATED_DETACHED_CONTEXT"}
            ]
        return True

    responses.respond(assistant_output("completed answer"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        compaction=ResponsesCompaction(
            client=client_for(compact),
            model="compact",
            mode="previous_response_id" if failure == "missing_anchor" else "auto",
            should_compact=predicate,
        ),
    )
    async with timeout(30), agent.session() as session:
        result = await (await session.submit("KEEP_ORIGINAL_CONTEXT")).get_result()
        assert result.final_output == "completed answer" and len(seen) == (
            0 if failure == "missing_anchor" else 1
        )
        saved = await session.save_state()
        assert "KEEP_ORIGINAL_CONTEXT" in saved.to_json()
        assert not any(row["type"] == "compacted" for row in saved.history)
    assert len(calls) == (1 if failure in ("client", "malformed_id") else 0)
    if failure == "client":
        assert "MUTATED_DETACHED_CONTEXT" not in json.dumps(calls[0])


@pytest.mark.parametrize(
    ("item", "message"),
    [
        pytest.param(
            {
                "type": "function_call",
                "call_id": "missing",
                "name": "historical",
                "arguments": "{}",
            },
            None,
            id="incomplete-tool-pair",
        ),
        pytest.param(
            {"type": "item_reference", "id": "msg_untrusted_compaction_pointer"},
            "new or modified instruction or control item",
            id="opaque-item-reference",
        ),
        *(
            pytest.param(
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": "UNTRUSTED_COMPACTION_INSTRUCTION"}],
                },
                "new or modified instruction or control item",
                id=role,
            )
            for role in ("developer", "system")
        ),
    ],
)
async def test_invalid_returned_window_fails_without_publishing(responses, item, message):
    async def compact(**body):
        return {"output": [item]}

    responses.respond(assistant_output("original"))
    responses.respond(assistant_output("continued"))
    config = ResponsesCompaction(
        client=client_for(compact), model="compact", should_compact=lambda context: False
    )
    async with (
        timeout(30),
        Agent(
            instructions="Answer.", provider=provider(responses), compaction=config
        ).session() as session,
    ):
        await (await session.submit("KEEP_ORIGINAL_CONTEXT")).get_result()
        with pytest.raises(NativeError, match=message):
            await session.compact()
        await (await session.submit("continue")).get_result()
        assert "KEEP_ORIGINAL_CONTEXT" in json.dumps(responses.requests[-1])
        assert not any(row["type"] == "compacted" for row in (await session.save_state()).history)
        assert "UNTRUSTED_COMPACTION_INSTRUCTION" not in json.dumps(responses.requests[-1])


async def test_cancel_joins_compact_callback_and_rejects_late_window(responses):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    exited = False

    async def compact(**body):
        nonlocal exited
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        exited = True
        return window()

    responses.respond(assistant_output("completed before compaction"))
    factory = ResponsesCompaction(
        client=client_for(compact), model="compact", should_compact=lambda context: True
    )
    async with (
        timeout(30),
        Agent(
            instructions="Answer.", provider=provider(responses), compaction=factory
        ).session() as session,
    ):
        turn = await session.submit("cancel target")
        result = asyncio.create_task(turn.get_result())
        try:
            await entered.wait()
            cancelling = asyncio.create_task(turn.cancel())
            await cancelled.wait()
            assert not cancelling.done() and not exited
            release.set()
            await cancelling
            with pytest.raises(TurnStoppedError):
                await result
            assert exited
            assert "RETAINED_SUMMARY_BLOCK" not in (await session.save_state()).to_json()
        finally:
            release.set()


@pytest.mark.parametrize("automatic", [False, True])
async def test_context_transform_redacts_borrowed_compaction_input(responses, automatic):
    secret = "COMPACTION_TOOL_SECRET_SENTINEL"
    calls = []
    transforms = []

    @function_tool
    def secret_tool() -> str:
        """Return a synthetic private value."""
        return secret

    async def compact(**body):
        calls.append(body)
        return window()

    def transform(context):
        transforms.append((context.purpose, context.model))
        return [json.loads(json.dumps(item).replace(secret, "redacted")) for item in context.items]

    responses.respond(function_call("secret_tool", {}, "secret-call"))
    responses.respond(assistant_output("answer"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        tools=[secret_tool],
        transform_context=transform,
        compaction=ResponsesCompaction(
            client=client_for(compact),
            model="separate-compactor",
            mode="input",
            should_compact=lambda context: automatic,
        ),
    )
    async with timeout(30), agent.session() as session:
        assert (await (await session.submit("go")).get_result()).final_output == "answer"
        if not automatic:
            await session.compact()
        assert len(calls) == 1
        assert secret not in json.dumps(calls[0]["input"])
        assert "redacted" in json.dumps(calls[0]["input"])
        assert secret in (await session.save_state()).to_json()
    assert [context for context in transforms if context[0] == "compaction"] == [
        ("compaction", "separate-compactor")
    ]


@pytest.mark.parametrize("modify_echo", [False, True])
async def test_compaction_transform_keeps_original_instruction_provenance(responses, modify_echo):
    original = "SYNTHETIC_COMPACTION_INSTRUCTION"
    redacted = "SYNTHETIC_REDACTED_INSTRUCTION"
    captured = []

    def transform(context):
        if context.purpose == "compaction":
            captured.extend(
                item
                for item in context.items
                if item.get("role") == "developer" and original in json.dumps(item)
            )
            return [
                json.loads(json.dumps(item).replace(original, redacted)) for item in context.items
            ]
        return None

    async def compact(**body):
        controls = [
            item
            for item in body["input"]
            if item.get("role") == "developer" and redacted in json.dumps(item)
        ]
        assert len(controls) == 2
        if modify_echo:
            controls[0] = json.loads(
                json.dumps(controls[0]).replace(redacted, "SYNTHETIC_INVENTED_INSTRUCTION")
            )
        result = window()
        result["output"][:0] = controls
        return result

    responses.respond(assistant_output("seed"))
    responses.respond(assistant_output("continued"))
    agent = Agent(
        instructions=original,
        provider=provider(responses),
        transform_context=transform,
        compaction=ResponsesCompaction(
            client=client_for(compact),
            model="compactor",
            mode="input",
            should_compact=lambda _: False,
        ),
    )
    async with timeout(30):
        async with agent.session() as session:
            await (await session.submit("seed")).get_result()
            await session.update(instructions=f"{original} current")
            if modify_echo:
                with pytest.raises(NativeError, match="new or modified instruction"):
                    await session.compact()
                assert "RETAINED_SUMMARY_BLOCK" not in (await session.save_state()).to_json()
                return
            await session.compact()
            saved = await session.save_state()
            compacted = next(row["payload"] for row in saved.history if row["type"] == "compacted")
            accepted = compacted["replacement_history"]
            assert len(captured) == 2 and all(item in accepted for item in captured)
            assert redacted not in json.dumps(accepted)
        current_agent = Agent(
            instructions=f"{original} current",
            provider=agent.provider,
            transform_context=transform,
            compaction=agent.compaction,
        )
        async with current_agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            result = await (await restored.submit("continue")).get_result()
            assert result.final_output == "continued"
            replay = responses.requests[-1]["input"]
            assert f"{original} current" in json.dumps(replay)
            assert redacted not in json.dumps(replay)


@pytest.mark.parametrize("mode", ["keep", "subset", "duplicate", "reordered"])
async def test_compaction_retains_only_captured_reference_occurrences(responses, mode):
    first = {"type": "item_reference", "id": "msg_captured_first"}
    second = {"type": "item_reference", "id": "msg_captured_second"}
    captured = [first, second, first]
    replacement = {
        "keep": captured,
        "subset": [second, first],
        "duplicate": [first, second, first, first],
        "reordered": [first, first, second],
    }[mode]

    async def compact(**body):
        assert [item for item in body["input"] if item["type"] == "item_reference"] == captured
        return {"output": replacement}

    responses.respond(assistant_output("original"))
    responses.respond(assistant_output("continued"))
    config = ResponsesCompaction(
        client=client_for(compact), model="compact", should_compact=lambda context: False
    )
    async with (
        timeout(30),
        Agent(
            instructions="Answer.", provider=provider(responses), compaction=config
        ).session() as session,
    ):
        snapshot = await session.history_snapshot()
        await session.transact_history(expected_revision=snapshot.revision, keep=0, items=captured)
        await (await session.submit("original request")).get_result()
        original = (await session.history_snapshot()).items
        if mode in ("duplicate", "reordered"):
            with pytest.raises(NativeError, match="new or modified instruction or control item"):
                await session.compact()
            assert (await session.history_snapshot()).items == original
        else:
            await session.compact()
        await (await session.submit("continue")).get_result()
    assert [
        item for item in responses.requests[-1]["input"] if item["type"] == "item_reference"
    ] == (captured if mode in ("duplicate", "reordered") else replacement)


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("kind", ["compaction", "reasoning"])
async def test_unverified_compactor_cannot_introduce_opaque_context(responses, captured, kind):
    opaque = {"type": kind, "encrypted_content": "FOREIGN_OPAQUE_CONTEXT"}
    if kind == "reasoning":
        opaque["summary"] = []

    async def compact(**body):
        return {
            "output": [item for item in body["input"] if item["type"] == kind]
            if captured
            else [opaque],
            "verified_provider_scope": True,
        }

    config = ResponsesCompaction(
        client=client_for(compact),
        model="compact",
        mode="input",
        should_compact=lambda context: False,
    )
    responses.respond(assistant_output("original"))
    responses.respond(assistant_output("continued"))
    async with Agent(
        instructions="Current instructions.", provider=provider(responses), compaction=config
    ).session() as session:
        if captured:
            snapshot = await session.history_snapshot()
            await session.transact_history(
                expected_revision=snapshot.revision, keep=0, items=[opaque]
            )
        await (await session.submit("original request")).get_result()
        before = (await session.history_snapshot()).items
        if captured:
            await session.compact()
        else:
            with pytest.raises(NativeError, match="new or modified instruction or control item"):
                await session.compact()
            assert (await session.history_snapshot()).items == before
        await (await session.submit("continue")).get_result()
    assert [
        item["encrypted_content"]
        for item in responses.requests[-1]["input"]
        if item["type"] == kind
    ] == ([opaque["encrypted_content"]] if captured else [])


async def test_provider_scope_change_during_input_compaction_rejects_new_opaque_context(responses):
    from tests.test_native_compaction_lineage import _matching_compaction_client

    async def compact(**body):
        client.api_key = "changed-compactor-key"
        return window(opaque=True)

    responses.respond(assistant_output("original"))
    responses.respond(assistant_output("continued"))
    async with _matching_compaction_client(responses, compact) as client:
        config = ResponsesCompaction(
            client=client, model="compact", mode="input", should_compact=lambda _: False
        )
        async with Agent(
            instructions="Current instructions.", provider=provider(responses), compaction=config
        ).session() as session:
            await (await session.submit("original request")).get_result()
            before = (await session.history_snapshot()).items
            with pytest.raises(NativeError, match="new or modified instruction or control item"):
                await session.compact()
            assert (await session.history_snapshot()).items == before
            await (await session.submit("continue")).get_result()
    assert "OPAQUE_COMPLETE_WINDOW" not in json.dumps(responses.requests[-1])


async def test_unverified_compactor_can_return_plain_reasoning(responses):
    async def compact(**body):
        return {
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "VISIBLE_SUMMARY"}],
                }
            ]
        }

    responses.respond(assistant_output("original"))
    responses.respond(assistant_output("continued"))
    config = ResponsesCompaction(
        client=client_for(compact), model="compact", mode="input", should_compact=lambda _: False
    )
    async with Agent(
        instructions="Current instructions.", provider=provider(responses), compaction=config
    ).session() as session:
        await (await session.submit("original request")).get_result()
        await session.compact()
        assert "VISIBLE_SUMMARY" in (await session.save_state()).to_json()
        await (await session.submit("continue")).get_result()
