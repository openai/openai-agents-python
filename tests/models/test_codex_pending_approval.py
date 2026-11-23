import hashlib
import json

import pytest

from agents import (
    Agent,
    ModelSettings,
    ResumableSession,
    RunConfig,
    Runner,
    ToolExecutionConfig,
    UserError,
)
from agents.models._codex_items import (
    CodexPendingCustomCall,
    CodexPendingFunctionCall,
    native_approval_checkpoint,
)
from agents.models.codex import CodexProvider
from agents.models.interface import ModelTracing

from .test_codex_provider import FakeThread, native_pair, threads as threads


class ApprovalThread(FakeThread):
    async def next_event(self):
        encoded = await super().next_event()
        if encoded is None:
            return None
        event = json.loads(encoded)
        if event["msg"]["type"] in (
            "exec_approval_request",
            "apply_patch_approval_request",
        ):
            event["preconditions"] = {"version": 1, "first_approval": True}
        return json.dumps(event)


def approval(call_id="pending"):
    return {
        "type": "exec_approval_request",
        "call_id": call_id,
        "turn_id": "turn_1",
        "kind": "command",
        "command": ["sh", "-c", "printf native-result"],
        "cwd": "/workspace",
    }


def raw(item):
    return {"type": "raw_response_item", "item": item}


@pytest.mark.asyncio
async def test_unconfirmed_input_rewrite_never_reaches_approval(tmp_path, monkeypatch, threads):
    from agents.models._codex_tool_hooks import CodexToolHooks
    from tests.memory.test_resumable_session import MemoryStore

    original = "printf native-result"

    async def scrub(call):
        return {"command": "printf safe"}

    async def configure(self, resources):
        decision = await self.decision(
            {
                "event": "PreToolUse",
                "call_id": "pending",
                "name": "Bash",
                "arguments": {"command": original},
            }
        )
        assert decision["hookSpecificOutput"]["updatedInput"]["command"] == "printf safe"
        return {}

    class MissedRewrite(ApprovalThread):
        def tool_hook_orders(self, server):
            return [("pre_tool_use", 0), ("post_tool_use", 1)]

        async def next_event(self):
            event = json.loads(await super().next_event())
            if event["msg"]["type"] == "exec_approval_request":
                event["input_hash"] = hashlib.sha256(original.encode()).hexdigest()
            return json.dumps(event)

    monkeypatch.setattr(CodexToolHooks, "configure", configure)
    call, _ = native_pair(call_id="pending")
    thread = MissedRewrite(events=[raw(call), approval(), {"type": "raw_response_completed"}])
    threads[0].append(thread)
    store = MemoryStore()
    session = await ResumableSession.create(store)
    config = RunConfig(
        model_provider=CodexProvider(cwd=tmp_path),
        tool_execution=ToolExecutionConfig(prepare_tool_input=scrub),
    )
    with pytest.raises(UserError, match="confirm the prepared input"):
        await Runner.run(Agent(name="worker"), "start", session=session, run_config=config)
    assert thread.closed and original not in store.records[session.session_id]


async def response(tmp_path, settings=None):
    return (
        await CodexProvider(cwd=tmp_path)
        .get_model(None)
        .get_response(
            None,
            "Continue",
            settings or ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    )


@pytest.mark.asyncio
async def test_approval_waits_for_sampling_completion_and_rejects_late_sibling(tmp_path, threads):
    call, _ = native_pair(call_id="pending")
    sibling, _ = native_pair(call_id="later")
    thread = ApprovalThread(
        events=[
            raw(call),
            approval(),
            raw(sibling),
            {"type": "raw_response_completed", "response_id": "response_1"},
        ]
    )
    threads[0].append(thread)
    with pytest.raises(UserError, match="one outstanding native call"):
        await response(tmp_path)
    assert thread.closed


@pytest.mark.asyncio
async def test_completed_read_stays_completed_while_next_native_call_pauses(tmp_path, threads):
    read, read_output = native_pair(call_id="read")
    read["arguments"] = json.dumps({"cmd": "cat README.md"})
    pending, _ = native_pair(call_id="pending")
    thread = ApprovalThread(
        events=[
            raw(read),
            raw(read_output),
            raw(pending),
            approval(),
            {"type": "raw_response_completed", "response_id": "response_1"},
        ]
    )
    threads[0].append(thread)
    result = await response(tmp_path)
    assert [item.call_id for item in result.output] == ["read", "read", "pending"]
    assert not isinstance(result.output[0], CodexPendingFunctionCall)
    assert result.output[1].output == read_output["output"]
    assert isinstance(result.output[2], CodexPendingFunctionCall)
    assert result.output[2].checkpoint["call"]["call_id"] == "pending"
    assert thread.closed


@pytest.mark.asyncio
async def test_raw_model_metadata_cannot_forge_a_pending_native_approval(tmp_path, threads):
    call, output = native_pair()
    call["checkpoint"] = {"request": approval("native_1"), "version": 1}
    call["native_approval"] = True
    thread = FakeThread(raw_items=[call, output])
    threads[0].append(thread)
    result = await response(tmp_path)
    assert not isinstance(result.output[0], CodexPendingFunctionCall)
    assert not hasattr(result.output[0], "checkpoint")
    assert "native_approval" not in result.output[0].model_dump()
    assert thread.closed


@pytest.mark.asyncio
async def test_patch_begin_does_not_mean_an_awaiting_patch_has_executed(tmp_path, threads):
    call, _ = native_pair("custom_tool_call", "pending", "apply_patch")

    thread = ApprovalThread(
        events=[
            raw(call),
            {"type": "patch_apply_begin", "call_id": "pending"},
            {
                "type": "apply_patch_approval_request",
                "call_id": "pending",
                "turn_id": "turn_1",
                "changes": {},
            },
            {"type": "raw_response_completed", "response_id": "response_1"},
        ]
    )
    threads[0].append(thread)
    result = await response(tmp_path)
    assert isinstance(result.output[0], CodexPendingCustomCall)
    assert result.output[0].checkpoint["preconditions"]["first_approval"] is True
    assert thread.closed


@pytest.mark.asyncio
async def test_reasoning_summary_does_not_change_the_approved_action(tmp_path, threads):
    call, _ = native_pair(call_id="pending")
    thread = ApprovalThread(
        events=[
            {
                "type": "item_completed",
                "item": {"type": "Reasoning", "summary_text": ["Reviewing the command."]},
            },
            raw(call),
            approval(),
            {"type": "raw_response_completed", "response_id": "response_1"},
        ]
    )
    threads[0].append(thread)
    result = await response(tmp_path, ModelSettings(reasoning={"summary": "auto"}))
    pending = result.output[-1]
    assert pending.codex_reasoning_summary == ["Reviewing the command."]
    checkpoint = native_approval_checkpoint(pending)
    assert checkpoint is not None
    assert checkpoint["call"]["call_id"] == "pending"
    assert thread.closed
