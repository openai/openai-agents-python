from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider, SessionState, function_tool
from agents.models import ProviderResponseChain
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call

_IDS = ["msg_external_first", "msg_external_second"]


def _selection() -> ProviderResponseChain:
    return {"type": "previous_response", "id": "resp_external", "item_ids": list(_IDS)}


@asynccontextmanager
async def reference_peer(rejection=None):
    model = ResponsesFixture()
    wire = []

    async def anchor(request):
        wire.append(("GET", request.path))
        assert request.headers["Authorization"] == "Bearer current-owner"
        if rejection == "owner":
            raise web.HTTPForbidden()
        output = [
            {"type": "message", "id": identity, "role": "assistant", "content": []}
            for identity in _IDS
        ]
        if rejection == "missing":
            output.pop()
        elif rejection == "duplicate":
            output.append(output[0])
        return web.json_response(
            {
                "id": request.match_info["identity"],
                "status": "completed",
                "output": output if request.match_info["identity"] == "resp_external" else [],
            }
        )

    async def submit(request):
        wire.append(("POST", request.path))
        assert request.headers["Authorization"] == "Bearer current-owner"
        if rejection == "sampling":
            await request.read()
            return web.json_response({"error": {"message": "Synthetic refusal"}}, status=400)
        return await model.handle(request)

    app = web.Application()
    app.router.add_get("/v1/responses/{identity}", anchor)
    app.router.add_post("/v1/responses", submit)
    async with TestServer(app, shutdown_timeout=1) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="current-owner",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            yield provider, model, wire


async def test_native_provider_item_references_follow_current_anchor_once():
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "found"

    async with timeout(30), reference_peer() as (provider, model, wire):
        model.respond(function_call("lookup", {}, "call_reference"))
        model.respond(assistant_output("First."))
        model.respond(assistant_output("Second."))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use lookup once.",
            provider=provider,
            tools=[lookup],
            model_settings={"store": True},
        )
        selection = _selection()
        async with agent.session(provider_history=selection) as session:
            assert "item_ids" in selection
            selection["item_ids"].clear()
            first = await session.submit("First.")
            assert (await first.get_result()).final_output == "First."
            second = await session.submit("Second.")
            assert (await second.get_result()).final_output == "Second."
        assert effects == ["lookup"]
        assert wire[:2] == [("GET", "/v1/responses/resp_external"), ("POST", "/v1/responses")]
        assert model.requests[0]["input"][:2] == [
            {"type": "item_reference", "id": identity} for identity in _IDS
        ]
        assert all(
            item.get("type") != "item_reference"
            for request in model.requests[1:]
            for item in request["input"]
        )
        assert [request["previous_response_id"] for request in model.requests] == [
            "resp_external",
            "resp_1",
            "resp_2",
        ]


@pytest.mark.parametrize("rejection", ["owner", "missing", "duplicate"])
async def test_native_provider_item_references_refuse_unconfirmed_ownership_before_sampling(
    rejection,
):
    async with timeout(30), reference_peer(rejection) as (provider, model, wire):
        agent = Agent(
            model="gpt-5.2",
            instructions="Answer.",
            provider=provider,
            model_settings={"store": True},
        )
        with pytest.raises(NativeError, match="Provider (history|anchor|item)"):
            await agent.run("Continue.", provider_history=_selection())
        assert wire == [("GET", "/v1/responses/resp_external")]
        assert model.requests == []


async def test_native_provider_item_reference_restore_requires_current_exact_declaration():
    async with timeout(30), reference_peer() as (provider, model, wire):
        model.respond(assistant_output("First."))
        model.respond(assistant_output("Restored."))
        agent = Agent(
            model="gpt-5.2",
            instructions="Answer.",
            provider=provider,
            model_settings={"store": True},
        )
        async with agent.session(provider_history=_selection()) as source:
            first = await source.submit("First.")
            await first.get_result()
            state = await source.save_state()
        assert "current-owner" not in state.to_json()
        state = SessionState.from_json(state.to_json())
        selection = state.provider_history
        assert selection == {"type": "previous_response", "id": "resp_1", "item_ids": _IDS}
        before = list(wire)
        for current in [None, {"type": "previous_response", "id": "resp_1"}]:
            with pytest.raises(
                RuntimeError, match="(current declaration|current anchor declaration)"
            ):
                async with agent.session(state=state, provider_history=current):
                    pass
            assert wire == before
        async with agent.session(state=state, provider_history=selection) as restored:
            resumed = await restored.submit("Continue.")
            assert (await resumed.get_result()).final_output == "Restored."
        assert wire[len(before)] == ("GET", "/v1/responses/resp_1")
        assert all(item.get("type") != "item_reference" for item in model.requests[-1]["input"])


async def test_native_provider_item_references_do_not_dispatch_stateless_fallback_after_refusal():
    async with timeout(30), reference_peer("sampling") as (provider, _model, wire):
        agent = Agent(
            model="gpt-5.2",
            instructions="Answer.",
            provider=provider,
            model_settings={"store": True},
        )
        with pytest.raises(NativeError):
            await agent.run("Continue.", provider_history=_selection())
        assert wire == [("GET", "/v1/responses/resp_external"), ("POST", "/v1/responses")]


@pytest.mark.parametrize(
    "selection",
    [
        {"type": "previous_response", "item_ids": _IDS},
        {"type": "conversation", "id": "conv_external", "item_ids": _IDS},
        {"type": "previous_response", "id": "resp_external", "item_ids": []},
        {
            "type": "previous_response",
            "id": "resp_external",
            "item_ids": ["duplicate", "duplicate"],
        },
        {"type": "previous_response", "id": "resp_external", "item_ids": [""]},
        {"type": "previous_response", "id": "resp_external", "item_ids": ["x"] * 4097},
    ],
)
def test_provider_reference_capture_rejects_invalid_scope_and_bounds(selection):
    from _agents_codex.models import _capture_provider_history

    with pytest.raises((TypeError, ValueError), match="Provider item IDs"):
        _capture_provider_history(selection)
