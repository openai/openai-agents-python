"""Replay original public checkpoints from a real earlier source/native pair."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import replace as replace_output
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from _agents_codex import _media
from agents import (
    Agent,
    FunctionTool,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    InteractionExpiredError,
    NativeProvider,
    PromptedOutput,
    RunBudget,
    ToolOutput,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request

_FIXTURES = Path(__file__).parent / "fixtures/checkpoint_upgrades"
_PRODUCER = _FIXTURES / "e283d9a5"
_POSIX_CAPTURE = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="Linux captures require compatible POSIX native paths",
)


def checkpoint(name: str) -> SessionState:
    manifest = json.loads((_PRODUCER / "manifest.json").read_text())
    case = next(case for case in manifest["cases"] if case["file"] == name + ".json")
    raw = (_PRODUCER / case["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == case["sha256"]
    saved = SessionState.from_json(raw.decode())
    assert saved.format_version == case["sdk_format_version"]
    assert saved.codex_revision == manifest["producer"]["codex_revision"]
    return saved


def provider(peer: ResponsesFixture) -> NativeProvider:
    return NativeProvider(
        base_url=peer.url, api_key="synthetic-checkpoint-consumer", supports_websockets=False
    )


def test_checkpoint_capture_manifest_has_original_producer_identity():
    manifest = json.loads((_PRODUCER / "manifest.json").read_text())
    assert manifest["producer"]["kind"] == "source-snapshot"
    assert manifest["producer"]["source_commit"] == "e283d9a5d6d73c5f8097423c18a42625b85bacdf"
    assert (
        hashlib.sha256((_FIXTURES / "capture.py").read_bytes()).hexdigest()
        == manifest["capture_script_sha256"]
    )
    for name in ("idle", "manual", "code"):
        checkpoint(name)


def test_portable_pending_input_keeps_order_and_detaches_changes():
    saved = checkpoint("manual")
    raw: dict[str, JsonValue] = {
        "type": "function_call_output",
        "call_id": "new-application-item",
        "output": "synthetic",
    }
    staged = saved.with_pending_input("queue this").with_pending_input([raw, raw])
    original_items = staged.pending_input
    raw["output"] = "changed"
    staged.pending_input[0]["role"] = "assistant"
    assert saved.pending_input == []
    assert "pending_input" not in json.loads(saved.to_json())["native_continuation"]
    assert original_items == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "queue this"}],
        },
        {"type": "function_call_output", "call_id": "new-application-item", "output": "synthetic"},
        {"type": "function_call_output", "call_id": "new-application-item", "output": "synthetic"},
    ]
    assert SessionState.from_json(staged.to_json()).pending_input == original_items
    assert staged.without_pending_input().to_json() == saved.to_json()


@pytest.mark.parametrize(
    "item_type", ["message", "function_call_output", "custom_tool_call_output"]
)
def test_portable_pending_input_enforces_audio_capacity(
    monkeypatch: pytest.MonkeyPatch, item_type: str
):
    saved = checkpoint("manual")
    part: dict[str, JsonValue] = {
        "type": "input_audio",
        "audio_url": "data:audio/wav;base64,AA==",
    }
    item: dict[str, JsonValue]
    if item_type == "message":
        item = {"type": item_type, "role": "user", "content": [part]}
    else:
        item = {"type": item_type, "call_id": "staged-audio-output", "output": [part]}
    monkeypatch.setattr(_media, "_MAX_AUDIO_INPUT_BYTES", len(json.dumps([item])))

    staged = saved.with_pending_input([item])
    assert SessionState.from_json(staged.to_json()).pending_input == [item]
    with pytest.raises(ValueError, match="Audio input exceeds"):
        saved.with_pending_input([item, item])
    with pytest.raises(ValueError, match="Audio input exceeds"):
        staged.with_pending_input([item])

    data = json.loads(staged.to_json())
    data["native_continuation"]["pending_input"].append(item)
    with pytest.raises(ValueError, match="Audio input exceeds"):
        SessionState.from_json(json.dumps(data))
    assert staged.pending_input == [item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("load_checkpoint", [False, True])
def test_portable_pending_input_rejects_nonfinite_values(value: float, load_checkpoint: bool):
    saved = checkpoint("manual")
    item: dict[str, JsonValue] = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "queued", "detail": {"score": value}}],
    }
    with pytest.raises(ValidationError, match="finite number"):
        if load_checkpoint:
            data = json.loads(saved.to_json())
            data["native_continuation"]["pending_input"] = [item]
            SessionState.from_json(json.dumps(data))
        else:
            saved.with_pending_input([item])
    assert saved.pending_input == []


def test_portable_pending_input_refuses_idle_and_exhausted_checkpoints():
    with pytest.raises(ValueError, match="requires a manual continuation"):
        checkpoint("idle").with_pending_input("idle")
    data = json.loads(checkpoint("manual").to_json())
    data.update(
        format_version=8,
        run_budget={"version": 1, "max_requests": 1, "admitted_requests": 1},
    )
    saved = SessionState.from_json(json.dumps(data))
    with pytest.raises(ValueError, match="no remaining model requests"):
        saved.with_pending_input("over budget")
    assert saved.pending_input == []


@_POSIX_CAPTURE
async def test_older_idle_checkpoint_continues_original_history(responses: ResponsesFixture):
    saved = checkpoint("idle")
    original = saved.to_json()
    responses.respond(assistant_output("continued-after-upgrade"))
    async with provider(responses) as current:
        agent = Agent(
            instructions="Remember checkpoint history.", model="gpt-5.5", provider=current
        )
        async with agent.session(state=saved) as session:
            assert session.id == saved.source_session_id
            assert not responses.requests
            result = await (await session.submit("idle-after-upgrade")).get_result()
            assert result.final_output == "continued-after-upgrade"
            retained = await session.save_state()
            assert retained.history[: len(saved.history)] == saved.history
    wire = json.dumps(responses.requests[0]["input"])
    for text in ("idle-before-upgrade", "remembered-before-upgrade", "idle-after-upgrade"):
        assert wire.count(text) == 1
    assert len(responses.requests) == 1
    assert saved.to_json() == original


@_POSIX_CAPTURE
@pytest.mark.parametrize("reject_input", [False, True])
async def test_staged_input_after_restored_approval(
    responses: ResponsesFixture, reject_input: bool
):
    effects: list[str] = []
    checked_input: list[object] = []
    staged_text = "staged input after a restored approval"

    @function_tool(needs_approval=True, approval_key="checkpoint.record.v1")
    def record_decision(value: str) -> str:
        effects.append(value)
        return "recorded"

    async def check_input(_turn: object, _context: object, _agent: object, value: object):
        checked_input.append(value)
        return GuardrailResult(tripwire_triggered=reject_input and staged_text in json.dumps(value))

    responses.respond(function_call("record_decision", {"value": "once"}, "remember-once"))
    async with timeout(20), provider(responses) as current:
        agent = Agent(
            instructions="Record after approval and answer.",
            model="gpt-5.5",
            provider=current,
            tools=[record_decision],
            input_guardrails=[InputGuardrail("staged-input", check_input)],
        )
        budget = None if reject_input else RunBudget(max_requests=2)
        async with agent.session() as source:
            await source.submit("Record once.", budget=budget)
            await next_request(source.interactions(), record_decision)
            saved = await source.suspend()
            assert not saved.remembered_approvals
        saved = saved.with_pending_input(staged_text)
        if reject_input:
            saved = saved.with_pending_input(
                [
                    {
                        "type": "function_call_output",
                        "call_id": "staged-application-output",
                        "output": "private application input",
                    }
                ]
            )
        else:
            responses.respond(assistant_output("done"))
        async with agent.session(state=saved, resume_budget=budget) as restored:
            approval = await next_request(restored.interactions(), record_decision)
            await approval.approve(always=True)
            resumed = restored.restored_turn
            assert resumed is not None
            if reject_input:
                with pytest.raises(GuardrailTripwire):
                    await resumed.get_result()
                raw = checked_input[-1]
                assert isinstance(raw, str)
                guarded = json.loads(raw)
                assert guarded[0]["content"] == [{"type": "input_text", "text": staged_text}]
                assert guarded[1]["call_id"] == "staged-application-output"
                retry = await restored.save_state()
                assert retry.pending_input == saved.pending_input
                assert [decision.key for decision in retry.remembered_approvals] == [
                    "checkpoint.record.v1"
                ]
            else:
                assert (await resumed.get_result()).final_output == "done"
    assert effects == ["once"]
    assert len(responses.requests) == (1 if reject_input else 2)
    if not reject_input:
        assert staged_text in json.dumps(responses.requests[1]["input"])


@_POSIX_CAPTURE
@pytest.mark.parametrize("approve", [False, True])
async def test_staged_checkpoint_cannot_grant_hosted_mcp_approval(
    responses: ResponsesFixture, approve: bool
):
    @function_tool(needs_approval=True)
    def record(value: str) -> str:
        return value

    responses.respond(function_call("record", {"value": "hold"}, "hold-call"))
    async with timeout(20), provider(responses) as current:
        agent = Agent(
            instructions="Wait for the application approval.",
            model="gpt-5.5",
            provider=current,
            tools=[record],
        )
        async with agent.session() as source:
            await source.submit("Record after approval.")
            await next_request(source.interactions(), record)
            saved = await source.suspend()
        payload = json.loads(saved.to_json())
        payload["native_continuation"]["pending_input"] = [
            {
                "type": "mcp_approval_response",
                "approval_request_id": "provider-approval",
                "approve": approve,
            }
        ]
        imported = SessionState.from_json(json.dumps(payload))
        restore = agent.session(state=imported)
        if approve:
            with pytest.raises(RuntimeError, match="require live SDK approval authority"):
                async with restore:
                    pytest.fail("Checkpoint restore accepted a staged positive MCP approval")
        else:
            async with restore as restored:
                assert restored.restored_turn is not None
    assert len(responses.requests) == 1


class LookupArgs(BaseModel):
    key: str


@_POSIX_CAPTURE
@pytest.mark.parametrize("mode", ["original", "staged", "rejected"])
async def test_older_manual_checkpoint_preserves_accepted_effect_and_output_contract(
    responses: ResponsesFixture, mode: str
):
    saved = checkpoint("manual")
    staged_text = "additional input after the manual decision"
    stage_input = mode != "original"
    rejected = mode == "rejected"
    if stage_input:
        saved = SessionState.from_json(saved.with_pending_input(staged_text).to_json())
    effects: list[str] = []
    guard_started = asyncio.Event()
    release_guard = asyncio.Event()
    guard_inputs: list[object] = []
    retry_state: SessionState | None = None
    private_reply = {"receipt": "staged-reply-application-data"}

    async def check_staged_input(
        _turn: object, _context: object, _agent: object, value: object
    ) -> GuardrailResult:
        guard_inputs.append(value)
        guard_started.set()
        await release_guard.wait()
        return GuardrailResult(tripwire_triggered=rejected)

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "recorded-" + key

    lookup = FunctionTool(
        name="lookup",
        description="Read",
        input_type=LookupArgs,
        output_type=str | ToolOutput,
    )
    async with provider(responses) as current:
        agent = Agent(
            instructions="Record and request a decision.",
            model="gpt-5.5",
            provider=current,
            tools=[record, lookup],
            output_type=PromptedOutput(int),
            input_guardrails=[InputGuardrail("staged-input", check_staged_input)]
            if stage_input
            else [],
        )
        async with agent.session(state=saved) as session:
            assert not responses.requests and effects == []
            async for event in session.interactions():
                request = lookup.match(event)
                if request is not None:
                    break
            else:
                pytest.fail("Missing restored manual request")
            assert request.call_id == "pending-lookup" and request.arguments.key == "pending"
            assert request.session_id == session.id
            if not rejected:
                responses.respond(assistant_output("19"))
            if stage_input:
                reply = asyncio.create_task(
                    request.respond(
                        replace_output(
                            ToolOutput.text("fresh-after-upgrade"), application_data=private_reply
                        )
                    )
                )
                try:
                    await asyncio.wait_for(guard_started.wait(), timeout=10)
                    assert not responses.requests
                    assert guard_inputs == [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": staged_text}],
                        }
                    ]
                finally:
                    release_guard.set()
                    await reply
            else:
                await request.respond("fresh-after-upgrade")
            with pytest.raises(InteractionExpiredError):
                await request.respond("duplicate")
            restored_turn = session.restored_turn
            assert restored_turn is not None
            if rejected:
                with pytest.raises(GuardrailTripwire):
                    await restored_turn.get_result()
                assert not responses.requests
                retry_state = await session.save_state()
                assert retry_state.pending_input == saved.pending_input
                assert [
                    entry.application_data
                    for entry in retry_state.application_tool_outputs
                    if entry.call_id == "pending-lookup"
                ] == [private_reply]
            else:
                assert (await restored_turn.get_result()).final_output == 19
        if retry_state is not None:
            responses.respond(assistant_output("19"))
            async with agent.session(state=retry_state.without_pending_input()) as retry:
                restored_turn = retry.restored_turn
                assert restored_turn is not None
                assert (await restored_turn.get_result()).final_output == 19
                settled = await retry.save_state()
                assert [
                    entry.application_data
                    for entry in settled.application_tool_outputs
                    if entry.call_id == "pending-lookup"
                ] == [private_reply]
    assert effects == [] and len(responses.requests) == 1
    assert "staged-reply-application-data" not in json.dumps(responses.requests[0])
    assert tool_output(responses.requests[0], "accepted-record") == "recorded-accepted"
    assert tool_output(responses.requests[0], "pending-lookup") == "fresh-after-upgrade"
    if stage_input and not rejected:
        request_items = responses.requests[0]["input"]
        reply_index = next(
            index
            for index, item in enumerate(request_items)
            if item.get("call_id") == "pending-lookup"
            and item.get("type") == "function_call_output"
        )
        staged_index = next(
            index
            for index, item in enumerate(request_items)
            if item.get("role") == "user"
            and any(part.get("text") == staged_text for part in item.get("content", ()))
        )
        assert reply_index < staged_index


@_POSIX_CAPTURE
async def test_older_code_checkpoint_retains_native_values(responses: ResponsesFixture):
    saved = checkpoint("code")
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "read-cell",
                "input": 'text(load("retained").value);',
            },
        }
    )
    responses.respond(assistant_output("read-after-upgrade"))
    async with provider(responses) as current:
        agent = Agent(
            instructions="Store and read checkpoint values.",
            model="gpt-5.5",
            provider=current,
            programmatic_tool_calling=True,
        )
        async with agent.session(state=saved) as session:
            assert not responses.requests
            before = await session.save_state()
            assert before.root_code_mode == saved.root_code_mode
            assert (
                await (await session.submit("read-after-upgrade")).get_result()
            ).final_output == "read-after-upgrade"
    assert "29" in tool_output(responses.requests[-1], "read-cell", "custom_tool_call_output")
    assert len(responses.requests) == 2


def test_checkpoint_upgrade_rejects_unknown_envelope_format():
    raw = json.loads((_PRODUCER / "idle.json").read_text())
    with pytest.raises(ValidationError):
        SessionState.from_json(json.dumps({**raw, "format_version": 999}))


@_POSIX_CAPTURE
async def test_checkpoint_upgrade_does_not_relax_revision_or_output_checks(
    responses: ResponsesFixture,
):
    raw = json.loads((_PRODUCER / "idle.json").read_text())
    async with provider(responses) as current:
        agent = Agent(
            instructions="Remember checkpoint history.", model="gpt-5.5", provider=current
        )
        with pytest.raises(RuntimeError, match="checkpoint core revision differs"):
            async with agent.session(
                state=SessionState.from_json(json.dumps({**raw, "codex_revision": "0" * 40}))
            ):
                pytest.fail("Foreign revision opened")
        manual = Agent(
            instructions="Record and request a decision.",
            model="gpt-5.5",
            provider=current,
            output_type=PromptedOutput(str),
        )
        with pytest.raises(ValueError, match="output"):
            async with manual.session(state=checkpoint("manual")):
                pytest.fail("Changed output contract opened")
    assert responses.requests == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native path validation")
async def test_windows_refuses_original_posix_checkpoint_before_model_work(
    responses: ResponsesFixture,
):
    saved = checkpoint("idle")
    original = saved.to_json()
    async with provider(responses) as current:
        agent = Agent(
            instructions="Remember checkpoint history.", model="gpt-5.5", provider=current
        )
        with pytest.raises(RuntimeError, match="AbsolutePathBuf deserialized without a base path"):
            async with agent.session(state=saved):
                pytest.fail("POSIX checkpoint opened on Windows")
    assert responses.requests == []
    assert saved.to_json() == original
