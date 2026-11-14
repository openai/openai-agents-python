"""Private ordinary MCP continuation witness; public suspension remains gated."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agents import (
    Agent,
    Allow,
    ApprovalDecision,
    ApprovalRequest,
    HttpMCP,
    InteractionExpiredError,
    PendingRequests,
    Session,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, mcp_call


async def approval(turn: Any) -> ApprovalRequest:
    pending = await turn.wait_for_request_or_result()
    assert isinstance(pending, PendingRequests)
    (request,) = pending.requests
    assert isinstance(request, ApprovalRequest)
    assert request._message.get("_sdk_question_id")
    return request


async def wait_for_candidate_boundary(source: Session[Any, Any]) -> None:
    # The approval can arrive before Core records the completed model response.
    status = await source.checkpoint_status(kind="manual_wait")
    while status.status == "not_ready":
        await asyncio.sleep(0.01)
        status = await source.checkpoint_status(kind="manual_wait")
    assert status.status == "ready", status.reasons


async def candidate_capture(source: Session[Any, Any]) -> SessionState:
    await wait_for_candidate_boundary(source)
    return SessionState.from_json((await source.suspend()).to_json())


def descriptor(url: str) -> HttpMCP[Any]:
    return HttpMCP(
        name="docs",
        url=url,
        tools=["find-record"],
        tool_name_overrides={"find-record": "reviewed"},
        approval_mode="preapproved",
        tool_approval_overrides={"find-record": "prompt"},
        cache_tools_list=False,
    )


@pytest.mark.parametrize("decision", ["allow_once", "deny"])
@pytest.mark.parametrize("reset_tool_choice", [False, True])
async def test_portable_ordinary_mcp_candidate_fresh_exact_decision(
    responses: ResponsesFixture, decision: str, reset_tool_choice: bool
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    sampled = mcp_call("reviewed", "mcp__docs", "portable-native")
    argument_bytes = ' { "key" : "43" } '
    sampled["item"]["arguments"] = argument_bytes
    responses.respond(sampled)
    responses.respond(assistant_output("settled through native decision"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.",
                tools=[descriptor(url)],
                provider=native_provider,
                model_settings={"tool_choice": "required", "reset_tool_choice": reset_tool_choice},
            )
            async with agent.session() as source:
                old = await approval(await source.submit("Read"))
                old_id = old._message["_sdk_question_id"]
                saved = await candidate_capture(source)
                with pytest.raises(InteractionExpiredError):
                    await old.respond("allow_once")
            (pending_data,) = json.loads(saved.to_json())["native_continuation"]["pending_mcp"]
            assert (pending_data["server_name"], pending_data["tool_name"]) == (
                "docs",
                "find-record",
            )
            assert (pending_data["callable_namespace"], pending_data["callable_name"]) == (
                "mcp__docs",
                "reviewed",
            )
            assert "tool" not in pending_data
            assert peer.calls == []
            async with agent.session(state=saved) as restored:
                assert restored.restored_turn is not None
                fresh = await approval(restored.restored_turn)
                assert fresh._message["_sdk_question_id"] != old_id
                assert fresh._message["_sdk_question_id"] not in (
                    pending_data["receipt"],
                    pending_data["token"],
                )
                assert len(responses.requests) == 1
                encoded = json.dumps(
                    {
                        "server_name": pending_data["server_name"],
                        "request_id": pending_data["request_id"],
                        "decision": "accept",
                        "content": None,
                        "meta": None,
                    }
                )
                assert not await restored._native._respond_child_question(
                    old_id, "resolve_elicitation", encoded
                )
                await restored._native.call("resolve_elicitation", encoded)
                assert await restored._native._child_question_pending(
                    fresh._message["_sdk_question_id"]
                )
                assert peer.calls == []
                async with agent.session() as wrong_owner:
                    with pytest.raises((ValueError, RuntimeError)):
                        await wrong_owner.respond(fresh.reply("allow_once"))
                with pytest.raises(ValueError, match="allow_once.*deny"):
                    await fresh.respond(ApprovalDecision(True, always=True))  # type: ignore[arg-type]
                assert peer.calls == []
                await fresh.respond(decision)  # type: ignore[arg-type]
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "settled through native decision"
                with pytest.raises(InteractionExpiredError):
                    await fresh.respond("allow_once")
            assert [(call["name"], call["arguments"]) for call in peer.calls] == (
                [("find-record", {"key": "43"})] if decision == "allow_once" else []
            )
            assert len(responses.requests) == 2
            assert responses.requests[0]["tool_choice"] == "required"
            if reset_tool_choice and decision == "allow_once":
                assert responses.requests[1].get("tool_choice", "auto") == "auto"
            history = responses.requests[1]["input"]
            calls = [
                item
                for item in history
                if item.get("type") == "function_call" and item.get("call_id") == "portable-native"
            ]
            outputs = [
                item
                for item in history
                if item.get("type") == "function_call_output"
                and item.get("call_id") == "portable-native"
            ]
            assert len(calls) == len(outputs) == 1
            assert calls[0]["arguments"] == argument_bytes
            assert (calls[0]["namespace"], calls[0]["name"]) == ("mcp__docs", "reviewed")
    finally:
        await native_provider.aclose()


async def test_portable_ordinary_mcp_refuses_live_root_terminal_before_claim(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(
        function_call("exec_command", {"cmd": "sleep 30", "yield_time_ms": 1000}, "background")
    )
    responses.respond(assistant_output("process running"))
    responses.respond(mcp_call("reviewed", "mcp__docs", "portable-live-terminal"))
    responses.respond(assistant_output("original MCP request settled"))
    async with (
        timeout(25),
        peer.serve() as url,
        ExecutorPeer(
            tmp_path / "remote",
            synthetic_process_output="process running",
            synthetic_process_stays_running=True,
        ).serve() as executor,
        provider(responses) as native_provider,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(
            instructions="Use the requested tool.",
            tools=[descriptor(url)],
            provider=native_provider,
        )
        async with agent.session(environment=declaration(executor)) as source:
            assert (await (await source.submit("Start a process")).get_result()).final_output == (
                "process running"
            )
            assert "Process running with session ID" in tool_output(
                responses.requests[1], "background"
            )
            turn = await source.submit("Read a record")
            pending = await approval(turn)
            with pytest.raises(RuntimeError, match="live background terminals"):
                await source.suspend()
            assert peer.calls == []
            await pending.respond("allow_once")
            assert (await turn.get_result()).final_output == "original MCP request settled"
            assert [call["name"] for call in peer.calls] == ["find-record"]


async def test_portable_ordinary_mcp_candidate_changed_declaration_before_effect(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(mcp_call("reviewed", "mcp__docs", "changed-declaration"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.", tools=[descriptor(url)], provider=native_provider
            )
            async with agent.session() as source:
                await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
            peer.annotations = {"destructiveHint": True}
            with pytest.raises(RuntimeError, match="declaration|source|catalog|changed"):
                async with agent.session(state=saved):
                    pytest.fail("A changed current declaration admitted the saved pending call")
            assert peer.calls == []
            assert len(responses.requests) == 1
    finally:
        await native_provider.aclose()


async def test_portable_ordinary_mcp_candidate_cancellation_keeps_no_grant(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(mcp_call("reviewed", "mcp__docs", "cancel-native"))
    responses.respond(assistant_output("fresh approval after cancellation"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.", tools=[descriptor(url)], provider=native_provider
            )
            async with agent.session() as source:
                await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
            async with agent.session(state=saved) as cancelled:
                assert cancelled.restored_turn is not None
                stale = await approval(cancelled.restored_turn)
                await cancelled.restored_turn.cancel(mode="force")
                with pytest.raises(InteractionExpiredError):
                    await stale.respond("allow_once")
                assert peer.calls == []
                assert len(responses.requests) == 1
            async with agent.session(state=saved) as restored:
                assert restored.restored_turn is not None
                fresh = await approval(restored.restored_turn)
                assert fresh._message["_sdk_question_id"] != stale._message["_sdk_question_id"]
                await fresh.respond("allow_once")
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "fresh approval after cancellation"
            assert len(peer.calls) == 1
    finally:
        await native_provider.aclose()


async def test_portable_ordinary_mcp_candidate_cancelled_reply_after_native_enqueue(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(mcp_call("reviewed", "mcp__docs", "cancelled-reply-native"))
    responses.respond(assistant_output("one native effect after cancelled reply"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.", tools=[descriptor(url)], provider=native_provider
            )
            async with agent.session() as source:
                await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
            async with agent.session(state=saved) as restored:
                assert restored.restored_turn is not None
                request = await approval(restored.restored_turn)
                actual = restored._native
                accepted, release = asyncio.Event(), asyncio.Event()

                class HeldReply:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(actual, name)

                    async def _respond_child_question(self, *args: Any) -> bool:
                        result = await actual._respond_child_question(*args)
                        assert result is True
                        accepted.set()
                        await release.wait()
                        return result

                restored._native = HeldReply()
                replying = asyncio.create_task(request.respond("allow_once"))
                try:
                    await accepted.wait()
                    replying.cancel()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await replying
                    assert (
                        await restored.restored_turn.get_result()
                    ).final_output == "one native effect after cancelled reply"
                    with pytest.raises(InteractionExpiredError):
                        await request.respond("allow_once")
                    assert len(peer.calls) == 1
                    assert len(responses.requests) == 2
                finally:
                    release.set()
                    restored._native = actual
                    await asyncio.gather(replying, return_exceptions=True)
    finally:
        await native_provider.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_thread_id", "11111111-1111-4111-8111-111111111111"),
        ("server_name", "other-docs"),
        ("tool_name", "reviewed"),
        ("callable_name", "find-record"),
        ("declaration_sha256", "0" * 64),
    ],
)
async def test_portable_ordinary_mcp_candidate_rejects_substituted_owner_or_declaration(
    responses: ResponsesFixture, field: str, value: str
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(mcp_call("reviewed", "mcp__docs", "substituted-native"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.", tools=[descriptor(url)], provider=native_provider
            )
            async with agent.session() as source:
                await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
            forged = json.loads(saved.to_json())
            forged["native_continuation"]["pending_mcp"][0][field] = value
            with pytest.raises((ValueError, RuntimeError)):
                async with agent.session(state=SessionState.from_json(json.dumps(forged))):
                    pytest.fail("A substituted pending owner or declaration admitted reentry")
            assert peer.calls == []
            assert len(responses.requests) == 1
    finally:
        await native_provider.aclose()


@pytest.mark.parametrize("spoof_approval_meta", [False, True])
async def test_portable_ordinary_mcp_candidate_peer_prefix_has_no_capture_authority(
    responses: ResponsesFixture, spoof_approval_meta: bool
) -> None:
    from tests.test_native_scoped_policies import AskingPeer

    call_id = "portable-peer-origin"
    peer = AskingPeer(
        form_request_id=f"mcp_tool_call_approval_{call_id}",
        form_meta={"codex_approval_kind": "mcp_tool_call"} if spoof_approval_meta else None,
        effect_before_prompt=True,
    )
    peer.names = ("find-record",)
    peer.annotations = None
    responses.respond(mcp_call("reviewed", "mcp__docs", call_id))
    responses.respond(assistant_output("finished peer form"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.",
                tools=[descriptor(url)],
                provider=native_provider,
            )
            async with agent.session() as source:
                turn = await source.submit("Read")
                original = await approval(turn)
                await wait_for_candidate_boundary(source)
                assert await source._native._manual_ready()
                assert peer.calls == []
                await original.respond("allow_once")
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                (remote,) = pending.requests
                await peer.prompted.wait()
                assert len(peer.calls) == 1
                assert remote._message["id"].startswith("codex-mcp-elicitation-")
                assert not isinstance(remote, ApprovalRequest)
                # The actual peer route has no TurnState sender identity. Its
                # prefix and metadata cannot capture an already-started effect.
                assert not await source._native._manual_ready()
                assert "_sdk_mcp_approval_scope" not in remote._message
                with pytest.raises(RuntimeError, match="settled manual boundary"):
                    await source.suspend()
                if isinstance(remote, ApprovalRequest):
                    await remote.respond("deny")
                else:
                    await remote.decline()
                await turn.get_result()
                assert len(peer.calls) == 1
                assert len(peer.answers) == 1
                assert peer.answers[0]["id"] == peer.form_request_id
    finally:
        await native_provider.aclose()


async def test_portable_ordinary_mcp_candidate_copied_snapshot_requires_separate_native_decisions(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(mcp_call("reviewed", "mcp__docs", "portable-forks"))
    responses.respond(assistant_output("denied independent fork"))
    responses.respond(assistant_output("approved independent fork"))
    native_provider = provider(responses)
    try:
        async with timeout(20), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.", tools=[descriptor(url)], provider=native_provider
            )
            async with agent.session() as source:
                old = await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
                with pytest.raises(InteractionExpiredError):
                    await old.respond("allow_once")
                assert peer.calls == []
            async with agent.session(state=saved) as left, agent.session(state=saved) as right:
                assert left.id == right.id == saved.source_session_id
                assert left.restored_turn is not None and right.restored_turn is not None
                a = await approval(left.restored_turn)
                b = await approval(right.restored_turn)
                assert a._message["_sdk_question_id"] != b._message["_sdk_question_id"]
                reply = a.reply("allow_once")
                with pytest.raises((ValueError, RuntimeError)):
                    await right.respond(reply)
                assert not await right._native._respond_child_question(
                    a._message["_sdk_question_id"],
                    "resolve_elicitation",
                    json.dumps(
                        {
                            "server_name": "docs",
                            "request_id": a._message["id"],
                            "decision": "accept",
                            "content": None,
                            "meta": None,
                        }
                    ),
                )
                assert peer.calls == []
                assert len(responses.requests) == 1
                await b.respond("deny")
                assert (
                    await right.restored_turn.get_result()
                ).final_output == "denied independent fork"
                assert peer.calls == []
                await left.respond(reply)
                assert (
                    await left.restored_turn.get_result()
                ).final_output == "approved independent fork"
                with pytest.raises((ValueError, RuntimeError)):
                    await left.respond(reply)
                assert len(peer.calls) == 1
    finally:
        await native_provider.aclose()


@pytest.mark.parametrize("replacement", ["endpoint", "callback"])
async def test_portable_ordinary_mcp_candidate_current_root_source_replacement_is_denied(
    responses: ResponsesFixture, replacement: str
) -> None:
    original_peer = Peer(names=("find-record",), annotations=None)
    other_peer = Peer(names=("find-record",), annotations=None)
    callbacks: list[str] = []

    def before(*_: Any) -> Allow:
        callbacks.append("before")
        return Allow()

    responses.respond(mcp_call("reviewed", "mcp__docs", "portable-current-source"))
    native_provider = provider(responses)
    try:
        async with timeout(15), original_peer.serve() as url, other_peer.serve() as other_url:
            agent = Agent(
                instructions="Read the record.", tools=[descriptor(url)], provider=native_provider
            )
            async with agent.session() as source:
                await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
            current = (
                descriptor(other_url)
                if replacement == "endpoint"
                else replace(descriptor(url), before=before)
            )
            changed = Agent(
                instructions="Read the record.", tools=[current], provider=native_provider
            )
            with pytest.raises((ValueError, RuntimeError)):
                async with changed.session(state=saved):
                    pytest.fail("A changed current native source was accepted")
            assert callbacks == []
            assert original_peer.calls == other_peer.calls == []
            assert len(responses.requests) == 1
    finally:
        await native_provider.aclose()


async def test_portable_ordinary_mcp_candidate_original_callback_source_cannot_be_parked(
    responses: ResponsesFixture,
) -> None:
    callbacks: list[str] = []

    def before(*_: Any) -> Allow:
        callbacks.append("before")
        return Allow()

    peer = Peer(names=("find-record",), annotations=None)
    responses.respond(mcp_call("reviewed", "mcp__docs", "portable-callback-source"))
    responses.respond(assistant_output("denied without callback replay"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(
                instructions="Read the record.",
                tools=[replace(descriptor(url), before=before)],
                provider=native_provider,
            )
            async with agent.session() as source:
                turn = await source.submit("Read")
                question = await approval(turn)
                assert callbacks == ["before"]
                assert not await source._native._manual_ready()
                with pytest.raises(RuntimeError, match="settled manual boundary"):
                    await source.suspend()
                await question.respond("deny")
                await turn.get_result()
                assert callbacks == ["before"]
                assert peer.calls == []
    finally:
        await native_provider.aclose()


@pytest.mark.parametrize("spoof_approval_meta", [False, True])
async def test_portable_ordinary_mcp_restored_call_keeps_peer_elicitation_owner(
    responses: ResponsesFixture, spoof_approval_meta: bool
) -> None:
    from tests.test_native_scoped_policies import AskingPeer

    call_id = "portable-peer-after-restore"
    peer = AskingPeer(
        form_request_id=f"mcp_tool_call_approval_{call_id}",
        form_meta={"codex_approval_kind": "mcp_tool_call"} if spoof_approval_meta else None,
        effect_before_prompt=True,
    )
    peer.names = ("find-record",)
    peer.annotations = None
    responses.respond(mcp_call("reviewed", "mcp__docs", call_id))
    responses.respond(assistant_output("settled restored peer form"))
    native_provider = provider(responses)
    try:
        async with timeout(15), peer.serve() as url:
            agent = Agent(instructions="Read.", tools=[descriptor(url)], provider=native_provider)
            async with agent.session() as source:
                old = await approval(await source.submit("Read"))
                saved = await candidate_capture(source)
            async with agent.session(state=saved) as restored:
                assert restored.restored_turn is not None
                fresh = await approval(restored.restored_turn)
                await fresh.respond("allow_once")
                pending = await restored.restored_turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                (remote,) = pending.requests
                assert not isinstance(remote, ApprovalRequest)
                assert remote._message["id"].startswith("codex-mcp-elicitation-")
                assert len(peer.calls) == 1
                assert not await restored._native._manual_ready()
                with pytest.raises(InteractionExpiredError):
                    await old.respond("allow_once")
                with pytest.raises(InteractionExpiredError):
                    await fresh.respond("allow_once")
                await remote.respond({"answer": "fresh peer decision"})
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "settled restored peer form"
                assert len(peer.calls) == len(peer.answers) == 1
                assert peer.answers[0]["id"] == peer.form_request_id
                assert len(responses.requests) == 2
    finally:
        await native_provider.aclose()
