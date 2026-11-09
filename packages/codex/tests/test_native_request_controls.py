"""Public native request controls against a real local Responses protocol peer."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, ModelSettings, NativeError, NativeProvider, ToolRef, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)

MODEL = "fixture-unlisted-request-controls"
CAPTURED_METADATA = {
    "workflow": "captured",
    **{f"tag-{index}": "value" for index in range(16)},
    "k" * 65: "v" * 513,
}
WIRE_FIELDS = (
    "max_output_tokens",
    "temperature",
    "top_p",
    "metadata",
    "prompt_cache_retention",
    "prompt_cache_options",
)


def provider_for(peer: ResponsesFixture, **options: Any) -> NativeProvider:
    return NativeProvider(
        base_url=peer.url, api_key="synthetic-controls-key", supports_websockets=False, **options
    )


def check_values(
    request: dict[str, Any], *, cap: int = 23, temperature: float | None = 0.4
) -> None:
    assert request["max_output_tokens"] == cap
    assert request.get("temperature") == temperature
    if temperature is None:
        assert "temperature" not in request
    assert request["top_p"] == 0.7
    assert request["parallel_tool_calls"] is False
    assert request["text"]["verbosity"] == "high"
    assert request["metadata"] == CAPTURED_METADATA
    assert request["store"] is True
    assert request["prompt_cache_retention"] == "24h"
    assert request["prompt_cache_options"] == {"ttl": "12h"}
    assert "request_controls" not in request


async def test_native_wire_capture_updates_and_restores_values_and_explicit_clears(
    responses: ResponsesFixture,
) -> None:
    for _ in range(5):
        responses.respond(assistant_output("finished"))
    metadata = dict(CAPTURED_METADATA)
    settings: ModelSettings = {
        "max_output_tokens": 23,
        "temperature": 0.4,
        "top_p": 0.7,
        "parallel_tool_calls": False,
        "verbosity": "high",
        "metadata": metadata,
        "store": True,
        "prompt_cache_retention": "24h",
        "prompt_cache_ttl": "12h",
    }
    async with timeout(40), provider_for(responses) as provider:
        agent = Agent(
            model=MODEL, instructions="Answer.", provider=provider, model_settings=settings
        )
        metadata["workflow"] = "mutated"
        settings["max_output_tokens"] = 999
        async with agent.session() as session:
            assert (await (await session.submit("initial")).get_result()).final_output == "finished"
            await session.update(model_settings={"max_output_tokens": 47, "temperature": None})
            await (await session.submit("patch")).get_result()
            saved = await session.save_state()
            await session.update(
                model_settings={
                    "max_output_tokens": None,
                    "top_p": None,
                    "parallel_tool_calls": None,
                    "verbosity": None,
                    "metadata": None,
                    "store": None,
                    "prompt_cache_retention": None,
                    "prompt_cache_ttl": None,
                }
            )
            await (await session.submit("cleared")).get_result()
            cleared = await session.save_state()
        async with agent.session(state=saved) as restored:
            await (await restored.submit("retained captured settings")).get_result()
        async with agent.session(state=cleared) as restored:
            await (await restored.submit("retain clears over Agent defaults")).get_result()
    check_values(responses.requests[0])
    check_values(responses.requests[1], cap=47, temperature=None)
    check_values(responses.requests[3], cap=47, temperature=None)
    for index in (2, 4):
        request = responses.requests[index]
        assert all(field not in request for field in WIRE_FIELDS)
        assert request["store"] is False and request["parallel_tool_calls"] is True
        assert request.get("text", {}).get("verbosity") is None
    assert len(responses.requests) == 5


@pytest.mark.parametrize(
    "ttl,retention",
    [(None, None), ("30m", "in_memory"), ("12h", "24h"), ("12h", None), (None, "24h")],
)
async def test_native_cache_controls_remain_independent_and_omission_stays_omitted(
    responses, ttl, retention
):
    responses.respond(assistant_output("finished"))
    selected = {
        key: value
        for key, value in (("prompt_cache_ttl", ttl), ("prompt_cache_retention", retention))
        if value is not None
    }
    async with timeout(25), provider_for(responses) as provider:
        await Agent(
            model=MODEL, instructions="Answer.", provider=provider, model_settings=selected
        ).run("cache controls")
    body = responses.requests[0]
    if ttl is None:
        assert "prompt_cache_options" not in body
    else:
        assert body["prompt_cache_options"] == {"ttl": ttl}
    if retention is None:
        assert "prompt_cache_retention" not in body
    else:
        assert body["prompt_cache_retention"] == retention


@pytest.mark.parametrize(
    "settings",
    [
        {"max_output_tokens": -1},
        {"max_output_tokens": True},
        {"max_output_tokens": 2**64},
        {"top_logprobs": 256},
        {"temperature": math.nan},
        {"temperature": 10**400},
        {"top_p": True},
        {"parallel_tool_calls": 1},
        {"store": "true"},
        {"verbosity": "LOUD"},
        {"metadata": {"x": 1}},
        {"prompt_cache_ttl": "24h"},
        {"prompt_cache_retention": "12h"},
    ],
)
def test_invalid_request_controls_fail_at_declaration(settings):
    with pytest.raises((ValueError, TypeError)):
        Agent(instructions="Never open resources.", model_settings=settings)


@pytest.mark.parametrize("cap,alternatives", [(0, 21), (2**64 - 1, 255)])
async def test_provider_owned_ranges_reach_the_wire_without_numeric_loss(
    responses: ResponsesFixture, cap: int, alternatives: int
) -> None:
    # This controlled provider accepts the request. Actual providers own semantic
    # ranges; these assertions only qualify the SDK's existing wire types.
    responses.respond(assistant_output("provider accepted"))
    async with timeout(25), provider_for(responses) as provider:
        result = await Agent(
            model=MODEL,
            instructions="Answer.",
            provider=provider,
            model_settings={
                "max_output_tokens": cap,
                "top_logprobs": alternatives,
                "temperature": 2.01,
                "top_p": -0.1,
            },
        ).run("Forward the caller's controls.")
    assert result.final_output == "provider accepted"
    assert len(responses.requests) == 1
    request = responses.requests[0]
    assert type(request["max_output_tokens"]) is int
    assert request["max_output_tokens"] == cap
    assert request["top_logprobs"] == alternatives
    assert request["temperature"] == 2.01
    assert request["top_p"] == -0.1


@pytest.mark.parametrize("ordinary", [False, True], ids=["atomic", "ordinary"])
async def test_explicit_verbosity_reaches_provider_despite_catalog_support_flag(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, ordinary: bool
) -> None:
    from agents import _native

    catalog = Path(__file__).parent / "fixtures" / "request-control-models.json"
    model = json.loads(catalog.read_text())["models"][0]
    assert model["slug"] == "gpt-5.5"
    assert model["support_verbosity"] is False
    assert model["use_responses_lite"] is False
    assert "used_fallback_model_metadata" not in model
    create_engine = _native.create_engine
    open_conversation = _native._open_conversation
    configured_paths: list[str] = []

    def configured_json(config_json: str) -> str:
        config = json.loads(config_json)
        assert "model_catalog_json" not in config
        config["model_catalog_json"] = str(catalog)
        return json.dumps(config)

    def configured_engine(config_json: str, *args: Any, **kwargs: Any) -> Any:
        configured_paths.append("ordinary")
        return create_engine(configured_json(config_json), *args, **kwargs)

    def configured_conversation(config_json: str, *args: Any, **kwargs: Any) -> Any:
        configured_paths.append("atomic")
        return open_conversation(configured_json(config_json), *args, **kwargs)

    monkeypatch.setattr(_native, "create_engine", configured_engine)
    monkeypatch.setattr(_native, "_open_conversation", configured_conversation)
    responses.respond(assistant_output("provider accepted verbosity"))
    async with timeout(25), provider_for(responses) as provider:
        result = await Agent(
            model="gpt-5.5",
            instructions="Answer.",
            provider=provider,
            model_settings={"verbosity": "high"},
        ).run("Forward explicit verbosity.", initial_messages=[] if ordinary else None)
    assert configured_paths == ["ordinary" if ordinary else "atomic"]
    assert result.final_output == "provider accepted verbosity"
    assert len(responses.requests) == 1
    assert responses.requests[0]["model"] == "gpt-5.5"
    assert responses.requests[0]["text"]["verbosity"] == "high"


@pytest.mark.parametrize("namespace", [None, "crm"])
async def test_named_choice_narrows_the_existing_catalog_and_literal_choice_clears_it(
    responses, namespace
):
    effects = []

    @function_tool(namespace=namespace)
    def selected() -> str:
        effects.append("selected")
        return "selected-result"

    @function_tool
    def excluded() -> str:
        effects.append("excluded")
        return "excluded-result"

    event = function_call("selected", {}, "selected-once")
    if namespace is not None:
        event["item"]["namespace"] = namespace
    responses.respond(event)
    responses.respond(assistant_output("finished"))
    responses.respond(assistant_output("literal cleared"))
    async with timeout(30), provider_for(responses) as provider:
        agent = Agent(
            model=MODEL,
            instructions="Select one tool.",
            tools=[selected, excluded],
            provider=provider,
            model_settings={"tool_choice": selected.ref},
        )
        async with agent.session() as session:
            await (await session.submit("named selection")).get_result()
            await session.update(model_settings={"tool_choice": "auto"})
            await (await session.submit("literal selection")).get_result()
    assert effects == ["selected"]
    for request in responses.requests[:2]:
        assert request["tool_choice"] == "required"
        [spec] = request["tools"]
        if spec["type"] == "namespace":
            assert spec["name"] == (namespace or "functions")
            assert [tool["name"] for tool in spec["tools"]] == ["selected"]
        else:
            assert namespace is None and spec["name"] == "selected"
    assert responses.requests[2]["tool_choice"] == "auto"
    assert "excluded" in str(responses.requests[2]["tools"])


@pytest.mark.parametrize("removed", ["unadmitted", "operation", "before_step", "deferred"])
async def test_named_target_cannot_widen_or_revive_a_removed_or_deferred_tool(responses, removed):
    @function_tool(defer_loading=removed == "deferred")
    def selected() -> str:
        pytest.fail("Rejected target must not execute")

    target = ToolRef("not-captured") if removed == "unadmitted" else selected.ref
    async with timeout(25), provider_for(responses) as provider:
        with pytest.raises((RuntimeError, ValueError), match="(named|tool|defer)"):
            async with Agent(
                model="gpt-5.5" if removed == "deferred" else MODEL,
                instructions="Select.",
                tools=[selected],
                provider=provider,
                model_settings={"tool_choice": target},
            ).session() as session:
                options = {"tools": []} if removed == "operation" else {}
                if removed == "before_step":
                    options["before_step"] = lambda _, *, step_index: []
                await (await session.submit("must reject", **options)).get_result()
    assert responses.requests == []


async def test_explicit_cap_incomplete_does_not_retry_or_replay_a_completed_tool(responses):
    effects = []

    @function_tool
    def committed() -> str:
        effects.append("one")
        return "committed-result"

    responses.respond(function_call("committed", {}, "committed-once"))
    responses.respond(assistant_output("partial"))

    async def limited(event):
        if event["type"] == "response.completed" and event["response"]["id"] == "resp_2":
            event["type"] = "response.incomplete"
            event["response"]["incomplete_details"] = {"reason": "max_output_tokens"}

    responses.before_event = limited
    async with timeout(30), provider_for(responses) as provider:
        async with Agent(
            model=MODEL,
            instructions="Act.",
            tools=[committed],
            provider=provider,
            model_settings={"max_output_tokens": 17},
        ).session() as session:
            with pytest.raises(NativeError, match="max_output_tokens"):
                await (await session.submit("limited")).get_result()
            turns = [turn async for turn in session.turns()]
    assert effects == ["one"] and len(responses.requests) == 2
    assert all(request["max_output_tokens"] == 17 for request in responses.requests)
    assert turns[-1]["responses"][-1]["status"] == "incomplete"
    assert turns[-1]["responses"][-1]["incomplete_reason"] == "max_output_tokens"


async def test_cap_reaches_existing_buffered_transport():
    from tests.test_buffered_responses import json_peer, json_response, provider

    async with timeout(30), json_peer() as peer:
        peer.json_replies.append(json_response("buffered", "buffered-cap", "buffered-item"))
        async with provider(peer) as connection:
            result = await Agent(
                model=peer.expected_model,
                instructions="Answer.",
                provider=connection,
                model_settings={"max_output_tokens": 19, "prompt_cache_ttl": "30m"},
            ).run("buffered")
        assert result.final_output == "buffered"
        assert peer.requests[0]["stream"] is False
        assert peer.requests[0]["max_output_tokens"] == 19
        assert peer.requests[0]["prompt_cache_options"] == {"ttl": "30m"}


async def test_controls_reach_existing_websocket_frames():
    from tests.test_native_provider_websockets import peer

    async with timeout(30), peer() as model:
        model.respond(assistant_output("websocket"))
        async with NativeProvider(
            base_url=model.url, api_key="synthetic-ws-controls", supports_websockets=True
        ) as provider:
            result = await Agent(
                model=MODEL,
                instructions="Answer.",
                provider=provider,
                model_settings={
                    "max_output_tokens": 29,
                    "temperature": 0.25,
                    "metadata": {"wire": "websocket"},
                    "prompt_cache_ttl": "12h",
                    "prompt_cache_retention": "in_memory",
                },
            ).run("websocket")
        assert result.final_output == "websocket"
        frames = [frame for frame in model.frames if frame.get("generate") is not False]
        assert frames and all(frame["max_output_tokens"] == 29 for frame in frames)
        assert frames[-1]["temperature"] == 0.25
        assert frames[-1]["metadata"] == {"wire": "websocket"}
        assert frames[-1]["prompt_cache_options"] == {"ttl": "12h"}
        assert frames[-1]["prompt_cache_retention"] == "in_memory"


async def test_known_lite_rejection_does_not_publish_a_model_or_controls_change(responses):
    responses.respond(assistant_output("still standard"))
    async with timeout(30), provider_for(responses) as provider:
        async with Agent(
            model=MODEL,
            instructions="Answer.",
            provider=provider,
            model_settings={"max_output_tokens": 31},
        ).session() as session:
            with pytest.raises(
                (ValueError, RuntimeError), match="standard Responses|Responses Lite"
            ):
                await session.update(model="gpt-5.6-sol")
            result = await (await session.submit("old settings remain")).get_result()
    assert result.final_output == "still standard"
    assert len(responses.requests) == 1 and responses.requests[0]["model"] == MODEL
    assert responses.requests[0]["max_output_tokens"] == 31


async def test_provider_rejection_of_unknown_model_control_is_not_dropped_or_retried():
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    requests = []

    async def reject(request):
        requests.append(await request.json())
        return web.json_response(
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "unsupported_parameter",
                    "message": "unsupported explicit temperature",
                }
            },
            status=400,
        )

    app = web.Application()
    app.router.add_post("/v1/responses", reject)
    async with timeout(30), TestServer(app, shutdown_timeout=1) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-rejection-key",
            supports_websockets=False,
        ) as provider:
            with pytest.raises(NativeError, match="unsupported explicit temperature"):
                await Agent(
                    model=MODEL,
                    instructions="Answer.",
                    provider=provider,
                    model_settings={"temperature": 0.4},
                ).run("unsupported")
    assert len(requests) == 1 and requests[0]["temperature"] == 0.4
