"""Captured routing and redirect boundaries through the public native provider."""

from __future__ import annotations

import pickle
from contextlib import asynccontextmanager
from copy import copy, deepcopy
from dataclasses import asdict

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, CodexLogin, NativeError, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output


@asynccontextmanager
async def route_peer():
    model = ResponsesFixture()
    queries = []
    paths = []
    redirect = {"location": None}

    async def handle(request):
        paths.append(request.path)
        queries.append(list(request.query.items()))
        if redirect["location"] is not None and len(model.requests) == 0:
            model.requests.append(await request.json())
            model.request_headers.append(
                {key.lower(): value for key, value in request.headers.items()}
            )
            return web.HTTPTemporaryRedirect(location=redirect["location"])
        return await model.handle(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    async with TestServer(app, shutdown_timeout=1) as server:
        model.url = str(server.make_url("/v1"))
        yield model, queries, paths, redirect


def test_provider_routing_capture_preserves_dataclass_copy_hash_pickle_and_secrecy(monkeypatch):
    monkeypatch.setenv("B02_ROUTE_ENV", "original-env-canary")
    headers = {"X-Route": "header-canary"}
    provider = NativeProvider(
        base_url="https://example.invalid/v1",
        api_key="key-canary",
        headers=headers,
        env_headers={"X-Env": "B02_ROUTE_ENV"},
        query_params={"query": "query-canary"},
        organization="org-canary",
        project="project-canary",
    )
    headers["X-Route"] = "mutated"
    monkeypatch.setenv("B02_ROUTE_ENV", "changed-env")
    assert asdict(provider)["headers"] == {"X-Route": "header-canary"}
    assert "canary" not in repr(provider)
    for duplicate in (copy(provider), deepcopy(provider), pickle.loads(pickle.dumps(provider))):
        assert duplicate == provider and hash(duplicate) == hash(provider)
    with pytest.raises(TypeError):
        provider.headers["X-Route"] = "changed"


async def test_native_routing_encodes_pairs_preserves_duplicates_and_captures_environment(
    monkeypatch,
):
    monkeypatch.setenv("B02_ROUTE_ENV", "env-original-canary")
    monkeypatch.delenv("B02_NEVER_SET", raising=False)
    async with timeout(35), route_peer() as (model, queries, paths, _):
        for _ in range(3):
            model.respond(assistant_output("routed"))
        headers = {"X-Route": "header-original-canary"}
        query = {
            "existing": "three",
            "reserved": "a&=+% /☃",
            "empty": "",
            "canary": "query-original-canary",
        }
        async with NativeProvider(
            base_url=model.url + "?existing=one&existing=two",
            api_key="synthetic-routing-key",
            supports_websockets=False,
            headers=headers,
            env_headers={"X-Env": "B02_ROUTE_ENV", "X-Missing": "B02_NEVER_SET"},
            query_params=query,
            organization="org-original-canary",
            project="project-original-canary",
        ) as provider:
            headers["X-Route"] = "mutated"
            query["reserved"] = "mutated"
            monkeypatch.setenv("B02_ROUTE_ENV", "env-changed-canary")
            agent = Agent(
                model="fixture-unlisted-routing", instructions="Answer.", provider=provider
            )
            async with agent.session() as session:
                await (await session.submit("first")).get_result()
                saved = await session.save_state()
            async with deepcopy(provider) as duplicate:
                await Agent(
                    model="fixture-unlisted-routing", instructions="Answer.", provider=duplicate
                ).run("copied captured provider")
            async with agent.session(state=saved) as restored:
                await (await restored.submit("restore current provider")).get_result()
            encoded = saved.to_json()
            for secret in (
                "header-original-canary",
                "env-original-canary",
                "query-original-canary",
                "org-original-canary",
                "project-original-canary",
            ):
                assert secret not in encoded and secret not in repr(provider)
        assert paths == ["/v1/responses"] * 3
        for pairs in queries:
            assert [value for key, value in pairs if key == "existing"] == ["one", "two", "three"]
            assert ("reserved", "a&=+% /☃") in pairs and ("empty", "") in pairs
        for actual in model.request_headers:
            assert (
                actual["x-route"] == "header-original-canary"
                and actual["x-env"] == "env-original-canary"
            )
            assert actual["openai-organization"] == "org-original-canary"
            assert actual["openai-project"] == "project-original-canary"
            assert "x-missing" not in actual and "x-sdk-sentinel-grant" not in actual


@pytest.mark.parametrize("origin", ["same", "foreign"])
@pytest.mark.parametrize("custom", [False, True])
async def test_native_redirect_policy_preserves_default_and_blocks_captured_routing(origin, custom):
    async with (
        timeout(35),
        route_peer() as (source, _, source_paths, redirect),
        route_peer() as (foreign, _, foreign_paths, _),
    ):
        source.respond(assistant_output("same-origin final"))
        foreign.respond(assistant_output("foreign final"))
        redirect["location"] = (source.url if origin == "same" else foreign.url) + "/redirected"
        options = {"headers": {"X-Route": "redirect-header-canary"}} if custom else {}
        async with NativeProvider(
            base_url=source.url,
            api_key="synthetic-redirect-key",
            supports_websockets=False,
            **options,
        ) as provider:
            agent = Agent(
                model="fixture-unlisted-routing", instructions="Answer.", provider=provider
            )
            if custom:
                with pytest.raises(NativeError):
                    await agent.run("must not follow")
                assert source_paths == ["/v1/responses"] and foreign_paths == []
                assert len(source.requests) == 1
            else:
                result = await agent.run("preserve default redirects")
                assert result.final_output == (
                    "same-origin final" if origin == "same" else "foreign final"
                )
                assert (len(source_paths), len(foreign_paths)) == (
                    (2, 0) if origin == "same" else (1, 1)
                )
        for actual in [*source.request_headers, *foreign.request_headers]:
            assert "x-sdk-sentinel-grant" not in actual
        if custom:
            assert source.request_headers[0]["x-route"] == "redirect-header-canary"


@pytest.mark.parametrize(
    "options",
    [
        {"headers": {"Authorization": "forged"}},
        {"headers": {"X-SDK-Sentinel-Grant": "forged"}},
        {"headers": {"X-Codex-Turn-State": "forged"}},
        {"headers": {"X-Route": "line\r\nbreak"}},
        {"headers": {"X-Route": "one"}, "env_headers": {"x-route": "B02_ENV"}},
        {"headers": {"OpenAI-Organization": "use-dedicated-field"}},
        {"organization": ""},
        {"query_params": {"bad": 7}},
        {"auth": CodexLogin(), "headers": {"X-Route": "unsupported"}},
    ],
)
def test_routing_rejects_transport_auth_conflicts_before_resource_open(options):
    with pytest.raises((TypeError, ValueError)):
        NativeProvider(**options)
