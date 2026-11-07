"""Replay original public checkpoints from a real earlier source/native pair."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from agents import (
    Agent,
    FunctionTool,
    InteractionExpiredError,
    NativeProvider,
    PromptedOutput,
    function_tool,
)
from agents.state import SessionState
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
    tool_output,
)

_FIXTURES = Path(__file__).parent / "fixtures/checkpoint_upgrades"
_PRODUCER = _FIXTURES / "e283d9a5"


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


class LookupArgs(BaseModel):
    key: str


async def test_older_manual_checkpoint_preserves_accepted_effect_and_output_contract(
    responses: ResponsesFixture,
):
    saved = checkpoint("manual")
    effects: list[str] = []

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "recorded-" + key

    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with provider(responses) as current:
        agent = Agent(
            instructions="Record and request a decision.",
            model="gpt-5.5",
            provider=current,
            tools=[record, lookup],
            output_type=PromptedOutput(int),
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
            responses.respond(assistant_output("19"))
            await request.respond("fresh-after-upgrade")
            with pytest.raises(InteractionExpiredError):
                await request.respond("duplicate")
            restored_turn = session.restored_turn
            assert restored_turn is not None
            assert (await restored_turn.get_result()).final_output == 19
    assert effects == [] and len(responses.requests) == 1
    assert tool_output(responses.requests[0], "accepted-record") == "recorded-accepted"
    assert tool_output(responses.requests[0], "pending-lookup") == "fresh-after-upgrade"


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


async def test_checkpoint_upgrade_does_not_relax_format_revision_or_output_checks(
    responses: ResponsesFixture,
):
    raw = json.loads((_PRODUCER / "idle.json").read_text())
    with pytest.raises(ValidationError):
        SessionState.from_json(json.dumps({**raw, "format_version": 999}))
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
