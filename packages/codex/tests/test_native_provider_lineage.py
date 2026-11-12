from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call


@asynccontextmanager
async def history_peer():
    model = ResponsesFixture()
    reads = []
    wire = []

    @web.middleware
    async def record_wire(request, handler):
        wire.append((request.method, request.path))
        return await handler(request)

    async def read_anchor(request):
        reads.append((request.path, request.headers.get("Authorization")))
        if request.headers.get("Authorization") != "Bearer current-owner":
            raise web.HTTPForbidden()
        identity = request.match_info["identity"]
        return web.json_response(
            {"id": identity, "status": "completed"}
            if request.match_info["kind"] == "responses"
            else {"id": identity, "object": "conversation"}
        )

    app = web.Application(middlewares=[record_wire])
    app.router.add_post("/v1/responses", model.handle)
    app.router.add_get("/v1/{kind}/{identity}", read_anchor)
    async with TestServer(app, shutdown_timeout=1) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="current-owner",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            yield provider, model, reads, str(server.make_url("/v1")), wire


async def test_native_response_lineage_sends_each_tool_effect_once():
    effects = []

    @function_tool
    def effect(label: str) -> str:
        effects.append(label)
        return label

    async with timeout(30), history_peer() as (provider, model, reads, _, _wire):
        model.respond(function_call("effect", {"label": "first"}, "call_first"))
        model.respond(function_call("effect", {"label": "second"}, "call_second"))
        model.respond(assistant_output("Finished."))
        agent = Agent(
            instructions="Perform both operations.",
            provider=provider,
            model="gpt-5.2",
            model_settings={"store": True},
            tools=[effect],
        )
        result = await agent.run(
            "Perform both operations.",
            provider_history={"type": "previous_response", "id": "resp_external"},
        )
        assert result.final_output == "Finished."
        assert effects == ["first", "second"]
        assert [row["previous_response_id"] for row in model.requests] == [
            "resp_external",
            "resp_1",
            "resp_2",
        ]
        assert all(row["store"] is True for row in model.requests)
        for row, identity in zip(model.requests[1:], ["call_first", "call_second"], strict=True):
            assert len(row["input"]) == 1
            assert row["input"][0]["type"] == "function_call_output"
            assert row["input"][0]["call_id"] == identity
        assert reads == [("/v1/responses/resp_external", "Bearer current-owner")]


async def test_native_conversation_keeps_its_id_without_enabling_response_storage():
    async with timeout(30), history_peer() as (provider, model, reads, _, _wire):
        model.respond(assistant_output("First."))
        model.respond(assistant_output("Second."))
        agent = Agent(
            instructions="Continue the selected conversation.",
            model="gpt-5.2",
            provider=provider,
            model_settings={"store": False},
        )
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            assert (await (await session.submit("First.")).get_result()).final_output == "First."
            assert (await (await session.submit("Second.")).get_result()).final_output == "Second."
        assert len(model.requests) == 2
        for row in model.requests:
            assert row["conversation"] == "conv_current"
            assert row["store"] is False
            assert "previous_response_id" not in row
        assert len(model.requests[1]["input"]) == 1
        assert model.requests[1]["input"][0]["content"][0]["text"] == "Second."
        assert reads == [("/v1/conversations/conv_current", "Bearer current-owner")]


async def test_native_portable_lineage_requires_current_anchor_and_account():
    async with timeout(45), history_peer() as (provider, model, reads, base_url, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(
            instructions="Continue the selected response.",
            model="gpt-5.2",
            provider=provider,
            model_settings={"store": True},
        )
        async with agent.session(provider_history={"type": "previous_response"}) as source:
            assert (await (await source.submit("Before.")).get_result()).final_output == "Before."
            state = await source.save_state()
            original = await source.history_snapshot()
        for selection in [None, {"type": "previous_response", "id": "resp_wrong"}]:
            with pytest.raises((NativeError, ValueError, RuntimeError)):
                async with agent.session(state=state, provider_history=selection):
                    raise AssertionError("an unauthorised anchor must fail before opening")
            assert len(model.requests) == 1
        assert not reads
        async with NativeProvider(
            base_url=base_url,
            api_key="different-owner",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        ) as other:
            refused = Agent(
                instructions="Continue the selected response.",
                model="gpt-5.2",
                provider=other,
                model_settings={"store": True},
            )
            with pytest.raises((NativeError, ValueError, RuntimeError)):
                async with refused.session(
                    state=state,
                    provider_history={"type": "previous_response", "id": "resp_1"},
                ) as session:
                    await (await session.submit("Never dispatch.")).get_result()
            assert len(model.requests) == 1
        async with agent.session(
            state=state, provider_history={"type": "previous_response", "id": "resp_1"}
        ) as restored:
            before = await restored.history_snapshot()
            assert before.items == original.items
            result = await (await restored.submit("After.")).get_result()
            assert result.final_output == "After."
        assert len(model.requests) == 2
        assert model.requests[1]["previous_response_id"] == "resp_1"
        assert len(model.requests[1]["input"]) == 1
        assert model.requests[1]["input"][0]["content"][0]["text"] == "After."
        assert reads == [
            ("/v1/responses/resp_1", "Bearer different-owner"),
            ("/v1/responses/resp_1", "Bearer current-owner"),
        ]


async def test_native_lineage_refuses_websocket_before_any_request():
    async with timeout(30), history_peer() as (_provider, _model, _reads, base_url, wire):
        async with NativeProvider(
            base_url=base_url,
            api_key="current-owner",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            agent = Agent(
                instructions="No dispatch.",
                model="gpt-5.2",
                provider=provider,
                model_settings={"store": True},
            )
            with pytest.raises((NativeError, ValueError, RuntimeError), match="WebSocket"):
                await agent.run(
                    "Never dispatch.",
                    provider_history={"type": "previous_response", "id": "resp_current"},
                )
        assert wire == []


@pytest.mark.parametrize("current_store", [False, None])
async def test_native_conversation_restore_rebinds_current_storage(current_store):
    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        source = Agent(
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            model_settings={"store": True},
        )
        mode = {"type": "conversation", "id": "conv_current"}
        async with source.session(provider_history=mode) as session:
            await (await session.submit("Before.")).get_result()
            state = await session.save_state()
        current = Agent(
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            model_settings={} if current_store is None else {"store": current_store},
        )
        async with current.session(state=state, provider_history=mode) as session:
            await (await session.submit("After.")).get_result()
        assert [request["store"] for request in model.requests] == [True, False]
        assert [request["conversation"] for request in model.requests] == ["conv_current"] * 2


async def test_native_response_chain_profile_restore_retains_current_storage():
    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        child = Agent(
            name="Other",
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            model_settings={"store": True},
        )
        agent = Agent(
            name="Root",
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            model_settings={"store": True},
            handoffs=[child],
        )
        async with agent.session(provider_history={"type": "previous_response"}) as session:
            await (await session.submit("Before.")).get_result()
            state = await session.save_state()
        async with agent.session(
            state=state, provider_history={"type": "previous_response", "id": "resp_1"}
        ) as session:
            await (await session.submit("After.")).get_result()
        assert [request["store"] for request in model.requests] == [True, True]
        assert model.requests[1]["previous_response_id"] == "resp_1"


@pytest.mark.parametrize("restore", [False, True])
async def test_native_rejected_lineage_compaction_keeps_session_usable(restore):
    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=provider)
        mode = {"type": "conversation", "id": "conv_current"}
        state = None
        if restore:
            async with agent.session(provider_history=mode) as original:
                await (await original.submit("Before.")).get_result()
                state = await original.save_state()
        async with agent.session(state=state, provider_history=mode) as session:
            if not restore:
                await (await session.submit("Before.")).get_result()
            before = await session.model_context()
            with pytest.raises(
                RuntimeError, match="Compaction is unavailable for provider-managed history"
            ):
                await session.compact(summary="Unpublished summary.")
            after = await session.model_context()
            assert after.items == before.items
            await (await session.submit("After.")).get_result()
        assert len(model.requests) == 2
        assert len(model.requests[1]["input"]) == 1
        assert model.requests[1]["input"][0]["role"] == "user"


async def test_native_rewritten_mcp_arguments_do_not_certify_remote_history():
    from agents import HttpMCP, ReplaceArguments
    from tests.test_native_mcp_tools import Peer, mcp_call

    peer = Peer(names=("search",))
    before_calls = []

    def before(_context, _call):
        before_calls.append(True)
        return ReplaceArguments({"key": "effective"})

    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        call = mcp_call("search", "mcp__records", "rewritten")
        call["item"]["arguments"] = '{"key":"original"}'
        model.respond(call)
        model.respond(assistant_output("Must not continue."))
        async with peer.serve() as url:
            agent = Agent(
                instructions="Search.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"store": True},
                tools=[
                    HttpMCP(
                        name="records",
                        url=url,
                        tools=["search"],
                        approval_mode="preapproved",
                        before=before,
                    )
                ],
            )
            async with agent.session(provider_history={"type": "previous_response"}) as session:
                with pytest.raises(NativeError, match="rewritten tool arguments"):
                    await (await session.submit("Search.")).get_result()
                assert before_calls == [True]
                assert len(peer.calls) <= 1
                assert all(call["arguments"] == {"key": "effective"} for call in peer.calls)
                assert len(model.requests) == 1
                with pytest.raises(NativeError):
                    await (await session.submit("Do not replay.")).get_result()
                assert len(model.requests) == 1


@pytest.mark.parametrize("restore,legacy", [(False, False), (True, False), (True, True)])
async def test_native_history_replacement_preserves_acknowledged_lineage(restore, legacy):
    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=provider)
        mode = {"type": "conversation", "id": "conv_current"}
        async with agent.session(provider_history=mode) as initial:
            await (await initial.submit("Before.")).get_result()
            state = await initial.save_state() if restore else None
            if legacy:
                import json

                saved = json.loads(state.to_json())
                removed = 0

                def without_boundary(value):
                    nonlocal removed
                    if isinstance(value, dict):
                        if "canonical_input" in value:
                            del value["canonical_input"]
                            removed += 1
                        for child in value.values():
                            without_boundary(child)
                    elif isinstance(value, list):
                        for child in value:
                            without_boundary(child)

                without_boundary(saved["history"])
                assert removed > 0
                state = type(state).from_json(json.dumps(saved))
            if restore:
                await initial.aclose()
            async with AsyncExitStack() as stack:
                session = (
                    await stack.enter_async_context(
                        agent.session(state=state, provider_history=mode)
                    )
                    if restore
                    else initial
                )
                before = await session.history_snapshot()
                with pytest.raises(RuntimeError, match="acknowledged provider history"):
                    await session.transact_history(
                        expected_revision=before.revision, keep=0, items=[]
                    )
                after = await session.history_snapshot()
                assert after.items == before.items
                assert after.revision == before.revision
                # An unsent addition may be appended and removed without rewriting
                # the acknowledged provider window.
                revision = await session.transact_history(
                    expected_revision=after.revision,
                    keep=after.total,
                    items=[
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Unsent draft."}],
                        }
                    ],
                )
                appended = await session.history_snapshot()
                assert appended.total == before.total + 1
                assert appended.revision == revision
                if legacy:
                    with pytest.raises(RuntimeError, match="acknowledged provider history"):
                        await session.transact_history(
                            expected_revision=revision, keep=before.total, items=[]
                        )
                else:
                    await session.transact_history(
                        expected_revision=revision, keep=before.total, items=[]
                    )
                    assert (await session.history_snapshot()).items == before.items
                await (await session.submit("After.")).get_result()
        assert len(model.requests) == 2


async def test_native_history_append_preserves_metadata_filtered_from_provider_input():
    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=provider)
        prior = {
            "type": "message",
            "id": "msg_prior",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "Prior.",
                    "annotations": [{"type": "future_annotation", "value": "retained"}],
                }
            ],
        }
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=initial.total, items=[prior]
            )
            await (await session.submit("Before.")).get_result()
            before = await session.history_snapshot()
            assert any(
                item.get("id") == "msg_prior"
                and item["content"][0]["annotations"] == prior["content"][0]["annotations"]
                for item in before.items
            )
            wire_prior = next(
                item for item in model.requests[0]["input"] if item.get("id") == "msg_prior"
            )
            assert wire_prior["content"][0].get("annotations", []) == []
            await session.transact_history(
                expected_revision=before.revision,
                keep=before.total,
                items=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Unsent draft."}],
                    }
                ],
            )
            after = await session.history_snapshot()
            assert after.items[: before.total] == before.items
            await (await session.submit("After.")).get_result()
        assert len(model.requests) == 2
