"""Accepted MCP changes affect future calls while retained Voice sources stay owned."""

from __future__ import annotations

import json
from contextlib import aclosing

import pytest

from agents import (
    Agent,
    Allow,
    HttpMCP,
    NativeError,
    NativeProvider,
    ReplaceArguments,
    StopTurn,
    TurnStoppedError,
    function_tool,
)
from agents.realtime import RealtimeConfig, RealtimeResponse
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_profile_mcp_metadata import profile_peer as profile_peer
from tests.test_native_realtime import realtime_peer as realtime_peer


def graph(model_provider, descriptor):
    return Agent(
        name="A",
        instructions="Use selected records.",
        model="gpt-5.5",
        tools=[descriptor],
        handoffs=[Agent(name="B", instructions="B answers.")],
        provider=model_provider,
    )


def descriptor(url, **kwargs):
    return HttpMCP(name="local", url=url, tools=["search"], approval_mode="preapproved", **kwargs)


@pytest.mark.parametrize("phase", ["before", "approval"])
@pytest.mark.parametrize("change", ["added", "changed", "removed"])
async def test_replacement_adds_changes_or_removes_only_current_policy(
    responses, profile_peer, phase, change
):
    peer, url = profile_peer
    callbacks = []
    results = []

    def original(ctx, call):
        callbacks.append("original")
        return Allow() if phase == "before" else True

    def replacement(ctx, call):
        callbacks.append("replacement")
        return StopTurn("private replacement stop")

    def result(ctx, value):
        results.append(ctx.call_id)
        return {
            "content": [{"type": "text", "text": "accepted replacement result"}],
            "isError": False,
        }

    opening = descriptor(url, **({phase: original} if change != "added" else {}))
    fresh = descriptor(
        url, transform_result=result, **({phase: replacement} if change != "removed" else {})
    )
    responses.respond(mcp_call("search", "mcp__local", "updated-call"))
    if change == "removed":
        responses.respond(assistant_output("completed"))
    responses.respond(assistant_output("reused"))
    async with timeout(25), graph(provider(responses), opening).session() as session:
        await session.update(tools=[fresh])
        turn = await session.submit("Use current policy")
        if change == "removed":
            assert (await turn.get_result()).final_output == "completed"
        else:
            with pytest.raises(TurnStoppedError) as caught:
                await turn.get_result()
            assert "private replacement stop" not in str(caught.value)
        assert callbacks == ([] if change == "removed" else ["replacement"])
        assert len(peer.calls) == (1 if change == "removed" else 0)
        assert results == (["updated-call"] if change == "removed" else [])
        assert (await (await session.submit("Continue")).get_result()).final_output == "reused"
    assert len(responses.requests) == (3 if change == "removed" else 2)
    if change == "removed":
        assert "accepted replacement result" in json.dumps(responses.requests[1])


async def test_replacement_controls_actual_arguments_metadata_and_result(responses, profile_peer):
    peer, url = profile_peer
    seen = []
    contexts = []
    application = {"tenant": "replacement"}
    secret = "PRIVATE_REPLACEMENT_METADATA_9027"

    def before(ctx, call):
        seen.append("before")
        contexts.append(ctx)
        return ReplaceArguments({"key": "replacement"})

    def metadata(ctx):
        seen.append("metadata")
        contexts.append(ctx)
        return {"app/replacement": secret}

    def result(ctx, value):
        seen.append("result")
        contexts.append(ctx)
        return {
            "content": [{"type": "text", "text": "accepted replacement only"}],
            "isError": False,
        }

    responses.respond(mcp_call("search", "mcp__local", "replaced-call"))
    responses.respond(assistant_output("accepted"))
    async with (
        timeout(25),
        graph(provider(responses), descriptor(url)).session(context=application) as session,
    ):
        await session.update(
            tools=[
                descriptor(url, before=before, request_metadata=metadata, transform_result=result)
            ]
        )
        turn = await session.submit("Use replacement")
        assert (await turn.get_result()).final_output == "accepted"
    assert seen == ["before", "metadata", "result"]
    assert all(ctx.context is application and ctx.submission is turn for ctx in contexts)
    assert dict(contexts[0].original_arguments) == {"key": "43"}
    assert dict(contexts[1].arguments) == {"key": "replacement"}
    assert len(peer.calls) == 1
    assert peer.calls[0]["arguments"] == {"key": "replacement"}
    assert peer.calls[0]["_meta"]["app/replacement"] == secret
    assert len(responses.requests) == 2
    encoded = json.dumps(responses.requests)
    assert secret not in encoded
    assert "accepted replacement only" in json.dumps(responses.requests[1])
    assert peer.label not in json.dumps(responses.requests[1])


async def test_empty_selection_does_not_restore_opening_mcp_or_callbacks(responses, profile_peer):
    peer, url = profile_peer
    seen = []

    def before(ctx, call):
        seen.append("before")
        return Allow()

    responses.respond(assistant_output("without tools"))
    async with (
        timeout(25),
        graph(provider(responses), descriptor(url, before=before)).session() as session,
    ):
        await session.update(tools=[])
        assert (
            await (await session.submit("No tools")).get_result()
        ).final_output == "without tools"
    assert seen == [] and peer.calls == []
    assert len(responses.requests) == 1
    assert all("search" not in json.dumps(tool) for tool in responses.requests[0].get("tools", []))


async def test_voice_retains_old_policy_while_ordinary_calls_use_replacement(
    realtime_peer, profile_peer
):
    voice = realtime_peer
    peer, url = profile_peer
    calls = []
    results = []
    old_turns = []

    def old_before(ctx, call):
        calls.append("old")
        return Allow()

    def new_before(ctx, call):
        calls.append("new")
        return StopTurn("private new stop")

    def old_result(ctx, value):
        results.append("old")
        old_turns.append(ctx.submission)
        return {"content": [{"type": "text", "text": "old captured result"}], "isError": False}

    def new_result(ctx, value):
        results.append("new")
        raise AssertionError("stopped call cannot publish a result")

    opening = descriptor(url, before=old_before, transform_result=old_result)
    replacement = descriptor(url, before=new_before, transform_result=new_result)
    voice.responses.respond(mcp_call("search", "mcp__local", "ordinary-new"))
    voice.responses.respond(assistant_output("reused"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-retained-policy",
            supports_websockets=False,
        ) as native_provider,
    ):
        async with graph(native_provider, opening).session() as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner:
                initial = await voice.frame(lambda frame: frame["type"] == "session.update")
                name = next(
                    tool["name"]
                    for tool in initial["session"]["tools"]
                    if "search" in tool.get("name", "")
                )
                assert voice.socket is not None
                await voice.socket.send_json(
                    {"type": "response.created", "response": {"id": "retained-response"}}
                )
                async with aclosing(owner.events()) as events:
                    async for event in events:
                        if (
                            isinstance(event, RealtimeResponse)
                            and event.status == "created"
                            and event.response_id == "retained-response"
                        ):
                            break
                await session.update(tools=[replacement])
                with pytest.raises(TurnStoppedError):
                    await (await session.submit("Use current policy")).get_result()
                assert calls == ["new"] and peer.calls == []
                item = {
                    "id": "retained-item",
                    "type": "function_call",
                    "name": name,
                    "call_id": "retained-call",
                    "arguments": json.dumps({"key": "old"}),
                }
                await voice.socket.send_json(
                    {
                        "type": "response.output_item.done",
                        "response_id": "retained-response",
                        "output_index": 0,
                        "item": item,
                    }
                )
                await voice.socket.send_json(
                    {
                        "type": "response.done",
                        "response": {"id": "retained-response", "output": [item]},
                    }
                )
                output = await voice.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "retained-call"
                )
                assert "old captured result" in output["item"]["output"]
                assert calls == ["new", "old"] and results == ["old"]
                assert len(peer.calls) == 1 and peer.calls[0]["arguments"] == {"key": "old"}
                await old_turns[0].get_result()
            assert (await (await session.submit("Continue")).get_result()).final_output == "reused"
    assert len(voice.responses.requests) == 2


async def test_function_only_update_replaces_callback_and_keeps_mcp_unoffered(
    responses, profile_peer
):
    peer, url = profile_peer
    effects = []

    @function_tool(name="local_lookup")
    def original(key: str) -> str:
        effects.append("old")
        return "old function result"

    @function_tool(name="local_lookup")
    def replacement(key: str) -> str:
        effects.append(key)
        return "new function result"

    opening_callbacks = []

    def opening_before(ctx, call):
        opening_callbacks.append(call)
        return Allow()

    opening = descriptor(url, before=opening_before)
    agent = Agent(
        name="A",
        model="gpt-5.5",
        instructions="Use current tools.",
        tools=[original, opening],
        handoffs=[Agent(name="B", instructions="B answers.")],
        provider=provider(responses),
    )
    responses.respond(function_call("local_lookup", {"key": "43"}, "local-new"))
    responses.respond(assistant_output("completed"))
    responses.respond(assistant_output("reused"))
    async with timeout(25), agent.session() as session:
        await session.update(tools=[replacement])
        assert (
            await (await session.submit("Call the current local function")).get_result()
        ).final_output == "completed"
        assert (await (await session.submit("Continue")).get_result()).final_output == "reused"
    assert effects == ["43"] and peer.calls == []
    assert opening_callbacks == []
    assert len(responses.requests) == 3
    assert all("search" not in json.dumps(tool) for tool in responses.requests[0].get("tools", []))
    assert "new function result" in json.dumps(responses.requests[1])


@pytest.mark.parametrize("fault", ["false-ack", "lost-ack"])
async def test_failed_catalog_ack_after_real_publication_fences_session(
    responses, profile_peer, fault
):
    peer, url = profile_peer
    responses.respond(assistant_output("must not be requested"))
    async with timeout(25), graph(provider(responses), descriptor(url)).session() as session:
        original = session._native
        committed_catalog = []

        class FaultyAcknowledgment:
            def __getattr__(self, name):
                return getattr(original, name)

            async def _replace_tools(self, generation, *args, **kwargs):
                accepted = await original._replace_tools(generation, *args, **kwargs)
                assert type(accepted) is int and accepted == generation
                committed_catalog.append(True)
                if fault == "lost-ack":
                    raise RuntimeError("controlled lost catalog acknowledgment")
                return False

        # Keep the existing publication path; corrupt only its post-commit receipt.
        session._native = FaultyAcknowledgment()
        with pytest.raises((NativeError, RuntimeError)):
            await session.update(tools=[descriptor(url, before=lambda ctx, call: Allow())])
        assert committed_catalog == [True]
        with pytest.raises(RuntimeError):
            await session.submit("A fenced session cannot continue")
    assert responses.requests == [] and peer.calls == []
