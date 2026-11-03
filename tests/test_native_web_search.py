"""Search admission at the actual native provider and dispatch boundaries."""

import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CapabilityError,
    LocalStorage,
    NativeAgents,
    NativeProvider,
    NativeRole,
    SessionState,
    WebSearch,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration


@asynccontextmanager
async def search_peer():
    model = ResponsesFixture()
    searches = []

    async def search(request):
        searches.append(
            {
                "body": await request.json(),
                "auth": request.headers.get("authorization"),
            }
        )
        return web.json_response({"output": "native-search-fixture-result", "results": None})

    app = web.Application()
    app.router.add_post("/v1/responses", model.handle)
    app.router.add_post("/v1/alpha/search", search)
    async with TestServer(app) as server:
        model.url = str(server.make_url("/v1"))
        yield model, searches


def search_call(code: bool, call_id: str):
    if code:
        return {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": call_id,
                "input": "text(await tools.web__run({search_query:[{q:'selected-search'}]}))",
            },
        }
    item = function_call("run", {"search_query": [{"q": "selected-search"}]}, call_id)
    item["item"]["namespace"] = "web"
    return item


@pytest.mark.parametrize("code", [False, True], ids=["direct", "code"])
@pytest.mark.parametrize("route", ["hosted", "standalone"])
@pytest.mark.parametrize("mode", ["disabled", "cached", "live"])
async def test_native_search_admission_and_real_dispatch(mode, route, code):
    async with timeout(25), search_peer() as (model, searches):
        model.respond(search_call(code, "web-1"))
        model.respond(assistant_output("done"))
        provider = NativeProvider(base_url=model.url, api_key="current-web-key")
        declarations = [] if mode == "disabled" else [WebSearch(mode=mode, route=route)]
        agent = Agent(
            instructions="Use only admitted capabilities.",
            model="gpt-5.5",
            provider=provider,
            tools=declarations,
            programmatic_tool_calling=code,
        )
        assert agent.validate() is None
        assert not searches and not model.requests
        async with provider, agent.session() as session:
            assert (
                await (await session.submit("try requested route")).get_result()
            ).final_output == "done"
        enabled = mode != "disabled" and route == "standalone"
        assert len(searches) == int(enabled)
        if enabled:
            assert searches[0]["auth"] == "Bearer current-web-key"
            assert "selected-search" in json.dumps(searches[0]["body"])
            assert searches[0]["body"]["settings"]["external_web_access"] is (mode == "live")
            assert "native-search-fixture-result" in json.dumps(model.requests[-1]["input"])
        else:
            assert "native-search-fixture-result" not in json.dumps(model.requests[-1]["input"])
        offered = model.requests[0]["tools"]
        if mode == "disabled":
            assert "web_search" not in json.dumps(offered)
            assert "web__run" not in json.dumps(offered)
            assert not any(t.get("name") == "web" for t in offered)
        elif route == "hosted":
            hosted = [t for t in offered if t.get("type", "").startswith("web_search")]
            assert len(hosted) == 1
            assert hosted[0]["external_web_access"] is (mode == "live")


async def test_new_search_grant_or_new_mode_rejected_before_io_and_does_not_fence():
    async with timeout(25), search_peer() as (model, searches):

        @function_tool
        async def original() -> str:
            return "original catalog"

        provider = NativeProvider(base_url=model.url, api_key="current-web-key")
        roster = [original]
        async with provider:
            async with Agent(
                instructions="Act", tools=roster, provider=provider
            ).session() as current:
                with pytest.raises(CapabilityError, match="captured"):
                    await current.submit("invalid", tools=[WebSearch()])
                with pytest.raises(CapabilityError, match="captured"):
                    await current.submit("invalid", tools=[original, WebSearch()])
                assert not model.requests and not searches
                model.respond(function_call("original", {}, "after-invalid"))
                model.respond(assistant_output("unchanged"))
                assert (
                    await (await current.submit("continue")).get_result()
                ).final_output == "unchanged"
                assert "original catalog" in json.dumps(model.requests[-1]["input"])
            admitted = [original, WebSearch(mode="cached")]
            async with Agent(
                instructions="Act", tools=admitted, provider=provider
            ).session() as current:
                with pytest.raises(CapabilityError, match="captured"):
                    await current.submit("invalid", tools=[original, WebSearch(mode="live")])
                assert not searches
                model.respond(function_call("original", {}, "kept"))
                model.respond(assistant_output("kept"))
                assert (
                    await (await current.submit("retained", tools=admitted)).get_result()
                ).final_output == "kept"


async def test_completed_web_search_restore_does_not_replay_or_restore_a_grant():
    async with timeout(30), search_peer() as (model, searches):
        provider = NativeProvider(base_url=model.url, api_key="captured-current-key")
        agent = Agent(
            instructions="Research.", tools=[WebSearch()], model="gpt-5.5", provider=provider
        )
        async with provider:
            async with agent.session() as source:
                model.respond(search_call(False, "original-search"))
                model.respond(assistant_output("known complete"))
                assert (
                    await (await source.submit("lookup")).get_result()
                ).final_output == "known complete"
                assert len(searches) == 1
                state = await source.save_state()
            restored = Agent(instructions="Research.", model="gpt-5.5", provider=provider)
            async with restored.session(state=state) as destination:
                model.respond(search_call(False, "ungranted"))
                model.respond(assistant_output("no new effect"))
                assert (
                    await (await destination.submit("again")).get_result()
                ).final_output == "no new effect"
                assert len(searches) == 1
                request = model.requests[-2]
                assert "web_search" not in json.dumps(request["tools"])
                assert not any(t.get("name") == "web" for t in request["tools"])


async def test_hosted_lite_refuses_without_standalone_dispatch():
    async with timeout(25), search_peer() as (model, searches):
        model.respond(search_call(True, "lite-hosted"))
        model.respond(assistant_output("not a hosted search"))
        provider = NativeProvider(base_url=model.url, api_key="selected-hosted-only")
        agent = Agent(
            instructions="Use only the selected hosted route.",
            model="gpt-5.6-sol",
            provider=provider,
            tools=[WebSearch(route="hosted")],
            programmatic_tool_calling=True,
        )
        rejected = False
        async with provider:
            try:
                async with agent.session() as current:
                    await (await current.submit("search")).get_result()
            except (ValueError, RuntimeError) as exc:
                rejected = True
                assert "hosted" in str(exc).lower(), str(exc)
        assert searches == [], searches
        assert not model.requests
        assert rejected, "The actual Lite metadata cannot use the declared hosted protocol"


async def test_hosted_rejects_a_lite_model_change_without_changing_the_original_grant():
    async with timeout(25), search_peer() as (model, searches):
        provider = NativeProvider(base_url=model.url, api_key="hosted-update")
        agent = Agent(
            instructions="Act",
            model="gpt-5.5",
            provider=provider,
            tools=[WebSearch(route="hosted")],
            programmatic_tool_calling=True,
        )
        async with provider, agent.session() as current:
            with pytest.raises(ValueError, match="hosted"):
                await current.update(model="gpt-5.6-sol")
            assert not model.requests and not searches
            model.respond(search_call(True, "not-granted"))
            model.respond(assistant_output("hosted survived"))
            assert (
                await (await current.submit("continue")).get_result()
            ).final_output == "hosted survived"
            assert model.requests[0]["model"] == "gpt-5.5"
            assert any(
                t.get("type", "").startswith("web_search") for t in model.requests[0]["tools"]
            )
            assert not searches


async def test_fresh_hosted_grant_refuses_a_saved_lite_model_before_automatic_restore():
    async with timeout(30), search_peer() as (model, searches):
        provider = NativeProvider(base_url=model.url, api_key="destination-hosted")
        async with provider:
            agent = Agent(instructions="Act", model="gpt-5.6-sol", provider=provider)
            model.respond(assistant_output("known receipt"))
            async with agent.session() as source:
                await (await source.submit("original")).get_result()
                state = await source.save_state()
            count = len(model.requests)
            restored = Agent(
                instructions="Act",
                model="gpt-5.5",
                provider=provider,
                tools=[WebSearch(route="hosted")],
            )
            with pytest.raises((ValueError, RuntimeError), match="hosted"):
                async with restored.session(state=state):
                    pytest.fail("The saved Lite model bypassed the destination's route")
            assert len(model.requests) == count and not searches


async def test_refused_hosted_local_resume_preserves_accepted_instructions(tmp_path):
    async with timeout(30), search_peer() as (model, searches):
        storage = LocalStorage(tmp_path / "profile")
        provider = NativeProvider(base_url=model.url, api_key="local-hosted-fixture")
        async with provider:
            source = Agent(
                instructions="Accepted source instructions.",
                model="gpt-5.6-sol",
                provider=provider,
            )
            model.respond(assistant_output("known original receipt"))
            async with source.session(storage=storage) as session:
                identity = session.id
                assert (
                    await (await session.submit("original")).get_result()
                ).final_output == "known original receipt"
            # LocalStorage's canonical native rollout is the durable result
            # under test. A later successful open publishes its own instructions,
            # so inspecting only a later model request would mask a failed write.
            [rollout] = list((storage.path / "sessions").rglob(f"*{identity}.jsonl"))

            def accepted():
                return [
                    row["payload"]
                    for line in rollout.read_text().splitlines()
                    if (row := json.loads(line))["type"] == "response_item"
                    and "sdk_application.accepted"
                    in (
                        (
                            row["payload"].get("internal_chat_message_metadata_passthrough") or {}
                        ).get("content_item_kinds")
                        or []
                    )
                ]

            original = accepted()
            assert original
            assert "Accepted source instructions." in json.dumps(original)
            count = len(model.requests)
            refused = Agent(
                instructions="Rejected destination must not be persisted.",
                # No model: the real saved Lite model must decide admission.
                model_settings={"reasoning_effort": "low"},
                provider=provider,
                tools=[WebSearch(route="hosted")],
            )
            with pytest.raises((ValueError, RuntimeError), match="hosted"):
                async with refused.session(storage=storage, resume=identity):
                    pytest.fail("Restored Lite was accepted for hosted search")
            assert accepted() == original
            assert len(model.requests) == count and not searches
            # An explicit compatible model must remain a supported way to
            # reopen. This also proves failed admission released the writer.
            compatible = Agent(
                instructions="Accepted destination instructions.",
                model="gpt-5.5",
                provider=provider,
                tools=[WebSearch(route="hosted")],
            )
            # Pinned Core compacts on this model change before the user turn.
            # The previous model summarizes; the compatible model answers.
            model.respond(assistant_output("Native summary: known original receipt."))
            model.respond(assistant_output("successor"))
            async with compatible.session(storage=storage, resume=identity) as restored:
                assert restored.id == identity
                assert (
                    await (await restored.submit("continue")).get_result()
                ).final_output == "successor"
            assert len(model.requests) == count + 2 and not searches
            compaction, continuation = model.requests[count:]
            assert compaction["model"] == "gpt-5.6-sol"
            assert not compaction.get("tools")
            assert continuation["model"] == "gpt-5.5"
            assert "Rejected destination" not in json.dumps(accepted())


@pytest.mark.parametrize(
    ("saved_model", "admitted"),
    [
        pytest.param("gpt-5.5", True, id="saved-compatible"),
        pytest.param("gpt-5.6-sol", False, id="saved-lite"),
    ],
)
async def test_hosted_local_resume_admits_saved_model_before_executor_io(
    tmp_path, monkeypatch, saved_model, admitted
):
    async with (
        timeout(30),
        search_peer() as (model, searches),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        storage = LocalStorage(tmp_path / "profile")
        provider = NativeProvider(base_url=model.url, api_key="stored-hosted-fixture")
        async with provider:
            source = Agent(instructions="Act", model=saved_model, provider=provider)
            model.respond(assistant_output("original receipt"))
            async with source.session(storage=storage) as session:
                identity = session.id
                assert (await (await session.submit("original")).get_result()).final_output == (
                    "original receipt"
                )
            initial_requests = len(model.requests)
            destination = Agent(
                instructions="Act",
                provider=provider,
                tools=[WebSearch(route="hosted")],
                programmatic_tool_calling=True,
            )
            assert executor.attempts == 0
            if admitted:
                model.respond(assistant_output("resumed receipt"))
                async with destination.session(
                    storage=storage, resume=identity, environment=declaration(executor)
                ) as resumed:
                    assert resumed.id == identity
                    assert (await (await resumed.submit("continue")).get_result()).final_output == (
                        "resumed receipt"
                    )
                assert executor.attempts > 0
                assert len(model.requests) == initial_requests + 1
                assert model.requests[-1]["model"] == saved_model
            else:
                with pytest.raises((ValueError, RuntimeError), match="hosted"):
                    async with destination.session(
                        storage=storage, resume=identity, environment=declaration(executor)
                    ):
                        pytest.fail("Stored Lite must not admit a hosted executor")
                assert executor.attempts == 0 and not executor.requests
                assert len(model.requests) == initial_requests
            assert not searches


async def test_hosted_validates_current_declared_native_child_models_before_executor_io(
    tmp_path, monkeypatch
):
    async with (
        timeout(30),
        search_peer() as (model, searches),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        provider = NativeProvider(base_url=model.url, api_key="hosted-children")
        agent = Agent(
            instructions="Act",
            model="gpt-5.5",
            provider=provider,
            tools=[
                WebSearch(route="hosted"),
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research",
                            model="gpt-5.6-sol",
                            instructions="Research.",
                            description="Research a record",
                        )
                    ]
                ),
            ],
            programmatic_tool_calling=True,
        )
        async with provider:
            with pytest.raises((ValueError, RuntimeError), match="hosted"):
                async with agent.session(environment=declaration(executor)):
                    pytest.fail("The child would use an ungranted protocol")
        assert not model.requests and not searches
        assert executor.attempts == 0 and not executor.requests


@pytest.mark.parametrize(
    ("selected_model", "route", "admitted"),
    [
        ("gpt-5.6-sol", "hosted", False),
        ("gpt-5.5", "hosted", True),
        ("gpt-5.6-sol", "standalone", True),
        pytest.param(None, "hosted", False, id="omitted-hosted"),
        pytest.param(None, "standalone", True, id="omitted-standalone"),
    ],
)
async def test_ordinary_search_model_admission_precedes_executor_io(
    tmp_path, monkeypatch, selected_model, route, admitted
):
    async with (
        timeout(30),
        search_peer() as (model, searches),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        provider = NativeProvider(base_url=model.url, api_key="search-admission-fixture")
        agent = Agent(
            instructions="Use only the selected search route.",
            model=selected_model,
            provider=provider,
            tools=[WebSearch(route=route)],
            programmatic_tool_calling=True,
        )
        async with provider:
            if admitted:
                async with agent.session(environment=declaration(executor)):
                    assert executor.attempts > 0
            else:
                with pytest.raises((ValueError, RuntimeError)) as error:
                    async with agent.session(environment=declaration(executor)):
                        pytest.fail("A hosted grant cannot open a Lite session")
                assert executor.attempts == 0 and not executor.requests
                assert not model.requests and not searches
                assert "hosted" in str(error.value).lower()
        assert not model.requests and not searches


async def test_hosted_restore_admission_uses_saved_model_not_opening_default():
    async with timeout(30), search_peer() as (model, searches):
        provider = NativeProvider(base_url=model.url, api_key="saved-hosted-fixture")
        async with provider:
            source_agent = Agent(instructions="Act", model="gpt-5.5", provider=provider)
            model.respond(assistant_output("saved with a compatible model"))
            async with source_agent.session() as source:
                await (await source.submit("original")).get_result()
                state = await source.save_state()
            count = len(model.requests)
            restored = Agent(
                instructions="Act",
                model="gpt-5.6-sol",
                provider=provider,
                tools=[WebSearch(route="hosted")],
            )
            # The native checkpoint owns the current model; opening defaults
            # cannot reject it before import. No new operation is submitted.
            async with restored.session(state=state):
                assert len(model.requests) == count and not searches


def test_duplicate_search_rejected_without_native_startup():
    with pytest.raises(ValueError, match="WebSearch only once"):
        Agent(instructions="Act", tools=[WebSearch(), WebSearch()]).validate()


@pytest.mark.parametrize(
    ("saved_model", "opening_model", "route", "admitted"),
    [
        ("gpt-5.6-sol", "gpt-5.5", "hosted", False),
        ("gpt-5.5", "gpt-5.6-sol", "hosted", True),
        ("gpt-5.6-sol", "gpt-5.5", "standalone", True),
        pytest.param("gpt-5.5", None, "hosted", True, id="saved-compatible-omitted"),
        pytest.param("gpt-5.6-sol", None, "hosted", False, id="saved-lite-omitted"),
    ],
)
async def test_portable_search_model_admission_precedes_executor_io(
    tmp_path, monkeypatch, saved_model, opening_model, route, admitted
):
    async with (
        timeout(30),
        search_peer() as (model, searches),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        provider = NativeProvider(base_url=model.url, api_key="portable-search-fixture")
        async with provider:
            original = Agent(instructions="Act", model=saved_model, provider=provider)
            model.respond(assistant_output("known receipt"))
            async with original.session() as source:
                assert (
                    await (await source.submit("original")).get_result()
                ).final_output == "known receipt"
                state = await source.save_state()
            count = len(model.requests)
            assert executor.attempts == 0
            restored = Agent(
                instructions="Act",
                model=opening_model,
                provider=provider,
                tools=[WebSearch(route=route)],
                programmatic_tool_calling=True,
            )
            if admitted:
                async with restored.session(state=state, environment=declaration(executor)):
                    assert executor.attempts > 0
            else:
                with pytest.raises((ValueError, RuntimeError), match="hosted"):
                    async with restored.session(state=state, environment=declaration(executor)):
                        pytest.fail("Saved Lite model cannot use the destination hosted grant")
                assert executor.attempts == 0 and not executor.requests
            assert len(model.requests) == count and not searches


@pytest.mark.parametrize("corruption", ["managed-specs", "root-identity", "none"])
async def test_hosted_portable_data_checked_before_executor_connect(
    tmp_path, monkeypatch, corruption
):
    async with (
        timeout(30),
        search_peer() as (model, searches),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        provider = NativeProvider(base_url=model.url, api_key="portable-preflight-fixture")
        async with provider:
            original = Agent(instructions="Act", model="gpt-5.5", provider=provider)
            model.respond(assistant_output("known receipt"))
            async with original.session() as source:
                assert (
                    await (await source.submit("original")).get_result()
                ).final_output == "known receipt"
                good = await source.save_state()
            count = len(model.requests)
            raw = json.loads(good.to_json())
            if corruption == "managed-specs":
                raw["managed_tool_specs"] = [{"name": "ungranted"}]
            elif corruption == "root-identity":
                raw["root_metadata"]["thread_id"] = "00000000-0000-4000-8000-000000000123"
            state = SessionState.from_json(json.dumps(raw))
            restored = Agent(
                instructions="Act",
                model="gpt-5.6-sol",
                provider=provider,
                tools=[WebSearch(route="hosted")],
                programmatic_tool_calling=True,
            )
            if corruption == "none":
                async with restored.session(state=state, environment=declaration(executor)):
                    assert executor.attempts > 0
            else:
                with pytest.raises((ValueError, RuntimeError)):
                    async with restored.session(state=state, environment=declaration(executor)):
                        pytest.fail("Invalid native data must not admit a connection")
                assert executor.attempts == 0 and not executor.requests
                # A rejected restore did not create or reserve the portable root.
                async with restored.session(state=good, environment=declaration(executor)):
                    assert executor.attempts > 0
            assert len(model.requests) == count and not searches
