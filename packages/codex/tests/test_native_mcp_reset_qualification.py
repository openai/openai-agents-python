"""Direct MCP replacement must keep provider and native history consistent."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import pytest

from agents import Agent, HttpMCP, ReplaceArguments
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_websockets import peer as websocket_peer


@pytest.mark.parametrize("upgrade_status", [None, 426], ids=["ws", "real-426"])
@pytest.mark.parametrize("binding", ["http", "stdio", "stdio-environment"])
@pytest.mark.parametrize("provider_id", [True, False], ids=["provider-id", "no-provider-id"])
async def test_replacement_invalidates_only_the_changed_response(
    upgrade_status, binding, provider_id, tmp_path, monkeypatch
):
    observed = []
    effective = {"key": "effective-reset-input"}
    original = {"key": "original-reset-input"}

    def before(ctx, call):
        assert call.route == "direct"
        assert ctx.call.call_id == call.call_id == "owned-mcp"
        assert json.loads(call.input) == original
        return ReplaceArguments(effective)

    def approval(ctx, call):
        observed.append((ctx.submission, json.loads(call.input)))
        return True

    log = tmp_path / "mcp-stdio"
    async with timeout(25), AsyncExitStack() as stack:
        remote = Peer(names=("search",))
        url = await stack.enter_async_context(remote.serve())
        model = await stack.enter_async_context(websocket_peer(upgrade_status=upgrade_status))
        provider = model.provider()
        environment = None
        if binding != "http":
            location = "host"
            if binding == "stdio-environment":
                executor = await stack.enter_async_context(
                    ExecutorPeer(tmp_path / "remote").serve()
                )
                trust(executor, tmp_path, monkeypatch)
                environment = declaration(executor)
                log = executor.root / "mcp-stdio"
                location = "environment"
            tool = stdio(log, location=location, before=before, approval=approval)
            namespace = "mcp__local"
        else:
            tool = HttpMCP(
                name="records", url=url, tools=["search"], before=before, approval=approval
            )
            namespace = "mcp__records"
        event = mcp_call("search", namespace, "owned-mcp")
        # This is an actual provider item ID, distinct from the stable call ID.
        if provider_id:
            event["item"]["id"] = "fc_provider_owned_mcp"
        else:
            event["item"].pop("id", None)
        event["item"]["arguments"] = json.dumps(original)
        model.respond(assistant_output("warm"))
        model.respond(event)
        model.respond(assistant_output("done"))
        model.respond(assistant_output("later"))
        agent = Agent(instructions="Search.", provider=provider, tools=[tool])
        async with agent.session(environment=environment) as session:
            assert (await (await session.submit("warm up")).get_result()).final_output == "warm"
            turn = await session.submit("search")
            assert (await turn.get_result()).final_output == "done"
            assert observed == [(turn, effective)]
            if binding == "http":
                calls = remote.calls
            else:
                calls = [
                    entry["message"]["params"]
                    for entry in records(log)
                    if entry.get("message", {}).get("method") == "tools/call"
                ]
            assert [call["arguments"] for call in calls] == [effective]
            inputs = model.requests[-1]["input"]
            called = [
                item
                for item in inputs
                if item.get("type") == "function_call" and item.get("call_id") == "owned-mcp"
            ]
            assert len(called) == 1 and json.loads(called[0]["arguments"]) == effective
            assert called[0].get("id") != "fc_provider_owned_mcp"
            assert (
                len(
                    [
                        item
                        for item in inputs
                        if item.get("type") == "function_call_output"
                        and item.get("call_id") == "owned-mcp"
                    ]
                )
                == 1
            )
            saved = await session.save_state()
            history = [
                row["payload"]
                for row in saved.history
                if row["type"] == "response_item" and row["payload"].get("call_id") == "owned-mcp"
            ]
            assert len([i for i in history if i["type"] == "function_call_output"]) == 1
            canonical = [i for i in history if i["type"] == "function_call"]
            assert len(canonical) == 1 and json.loads(canonical[0]["arguments"]) == effective
            if upgrade_status is None:
                assert not model.requests[-1].get("previous_response_id"), model.frames
                assert model.connections == 1 and not model.http_requests and not model.errors
            else:
                assert len(model.upgrades) == 1 and not model.frames
                assert len(model.http_requests) == len(model.requests) == 3
            assert (await (await session.submit("another")).get_result()).final_output == "later"
            # A later root may safely reuse the corrected prefix, no second tool effect.
            if binding == "http":
                assert [call["arguments"] for call in remote.calls] == [effective]
            else:
                assert (
                    len(
                        [
                            entry
                            for entry in records(log)
                            if entry.get("message", {}).get("method") == "tools/call"
                        ]
                    )
                    == 1
                )
        if binding != "http":
            await assert_stopped(log)


@pytest.mark.parametrize("upgrade_status", [None, 426], ids=["ws", "real-426"])
@pytest.mark.parametrize("binding", ["http", "stdio"])
@pytest.mark.parametrize("permit", [True, False], ids=["approved", "denied"])
async def test_pending_mcp_edit_cannot_change_shared_provider_sibling_or_replay(
    upgrade_status, binding, permit, tmp_path
):
    import asyncio

    from agents import NativeError

    held = asyncio.Event()
    release = asyncio.Event()
    original = {"key": "owner-private-original"}
    effective = {"key": "owner-private-effective"}
    decisions = []

    def before(ctx, call):
        assert call.route == "direct" and call.call_id == "owner-call"
        assert json.loads(call.input) == original
        return ReplaceArguments(effective)

    async def approval(ctx, call):
        decisions.append((ctx.submission, json.loads(call.input)))
        held.set()
        await release.wait()
        return permit

    log = tmp_path / "sibling-stdio"
    async with timeout(25), AsyncExitStack() as stack:
        remote = Peer(names=("search",))
        url = await stack.enter_async_context(remote.serve())
        model = await stack.enter_async_context(websocket_peer(upgrade_status=upgrade_status))
        provider = model.provider()
        if binding == "stdio":
            tool = stdio(log, before=before, approval=approval)
            namespace = "mcp__local"
        else:
            tool = HttpMCP(
                name="records", url=url, tools=["search"], before=before, approval=approval
            )
            namespace = "mcp__records"
        owner = await stack.enter_async_context(
            Agent(instructions="Own the MCP lookup.", provider=provider, tools=[tool]).session()
        )
        sibling = await stack.enter_async_context(
            Agent(instructions="SIBLING_ONLY_CONTEXT", provider=provider, tools=[]).session()
        )
        model.respond(assistant_output("owner warmed"))
        model.respond(assistant_output("sibling warmed"))
        assert (
            await (await owner.submit("owner first")).get_result()
        ).final_output == "owner warmed"
        assert (
            await (await sibling.submit("sibling first")).get_result()
        ).final_output == "sibling warmed"
        call_event = mcp_call("search", namespace, "owner-call")
        call_event["item"]["id"] = "fc_owner_only"
        call_event["item"]["arguments"] = json.dumps(original)
        model.respond(call_event)
        model.respond(assistant_output("sibling unaffected"))
        turn = await owner.submit("OWNER_ONLY_CONTEXT")
        await held.wait()
        assert decisions == [(turn, effective)]
        assert not remote.calls
        assert not [
            entry
            for entry in records(log)
            if entry.get("message", {}).get("method") == "tools/call"
        ]
        result = await (await sibling.submit("sibling second")).get_result()
        assert result.final_output == "sibling unaffected"
        sibling_input = model.requests[-1]["input"]
        assert "owner-private" not in json.dumps(sibling_input)
        assert "OWNER_ONLY_CONTEXT" not in json.dumps(sibling_input)
        if upgrade_status is None:
            # Pending owner invalidation cannot retire a sibling's warm WS prefix.
            assert model.requests[-1].get("previous_response_id")
        model.respond(assistant_output("owner complete"))
        release.set()
        if permit:
            assert (await turn.get_result()).final_output == "owner complete"
            current = model.requests[-1]
            actual = [
                item
                for item in current["input"]
                if item.get("type") == "function_call" and item.get("call_id") == "owner-call"
            ]
            assert len(actual) == 1 and json.loads(actual[0]["arguments"]) == effective
            if upgrade_status is None:
                assert not current.get("previous_response_id")
        else:
            with pytest.raises(NativeError):
                await turn.get_result()
            # Reuse the unused scripted response only on an explicit later turn.
            assert (
                await (await owner.submit("fresh owner turn")).get_result()
            ).final_output == "owner complete"
        calls = (
            remote.calls
            if binding == "http"
            else [
                entry["message"]["params"]
                for entry in records(log)
                if entry.get("message", {}).get("method") == "tools/call"
            ]
        )
        assert [c["arguments"] for c in calls] == ([effective] if permit else [])
        model.respond(assistant_output("sibling after"))
        assert (
            await (await sibling.submit("sibling third")).get_result()
        ).final_output == "sibling after"
        assert "owner-private" not in json.dumps(model.requests[-1]["input"])
        assert len(decisions) == 1
        if upgrade_status is None:
            assert not model.http_requests and not model.errors
            assert model.connections == 2
        else:
            assert not model.frames and model.http_requests
