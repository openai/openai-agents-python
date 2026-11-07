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
from agents.observation import ModelOutputItemCompleted
from agents.tools import WebSearchLocation
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


async def test_hosted_controls_are_captured_and_outer_choice_survives_idle_restore():
    async with timeout(30), search_peer() as (model, searches):
        allowed = ["weather.example"]
        blocked = ["ads.weather.example"]
        location: WebSearchLocation = {
            "country": "US",
            "region": "Illinois",
            "city": "Chicago",
            "timezone": "America/Chicago",
        }
        web_search = WebSearch(
            route="hosted", allowed_domains=allowed, blocked_domains=blocked, location=location
        )
        provider = NativeProvider(base_url=model.url, api_key="hosted-controls-fixture")
        agent = Agent(
            instructions="Answer the user's question.",
            model="gpt-5.5",
            provider=provider,
            tools=[web_search],
        )
        async with provider:
            async with agent.session() as session:
                allowed[:] = ["changed.example"]
                blocked.clear()
                location["timezone"] = "America/New_York"
                replacement = WebSearch(route="hosted", allowed_domains=["replacement.example"])
                with pytest.raises(CapabilityError, match="captured"):
                    await session.submit("invalid", tools=[replacement])
                with pytest.raises(CapabilityError, match="captured"):
                    await session.update(tools=[replacement])
                assert not model.requests

                model.respond(assistant_output("No lookup needed."))
                turn = await session.submit(
                    {"role": "user", "content": [{"type": "input_text", "text": "Remember Ada."}]},
                    tools=[web_search],
                )
                assert (await turn.get_result()).final_output == "No lookup needed."
                assert not [
                    item async for item in session.items() if item["type"] == "web_search_call"
                ]

                await session.update(model_settings={"tool_choice": "none"})
                model.respond(assistant_output("Ada is remembered."))
                turn = await session.submit("Whom did I ask you to remember?", tools=[web_search])
                assert (await turn.get_result()).final_output == "Ada is remembered."
                state = await session.save_state()

            model.respond(assistant_output("Still remembered."))
            async with agent.session(state=state) as restored:
                turn = await restored.submit(
                    "Confirm the name.", before_step=lambda turn, step_index: []
                )
                assert (await turn.get_result()).final_output == "Still remembered."

        assert not searches
        first, second, third = model.requests
        assert [request["tool_choice"] for request in model.requests] == ["auto", "none", "none"]
        tool = next(tool for tool in first["tools"] if tool["type"] == "web_search")
        assert tool == next(tool for tool in second["tools"] if tool["type"] == "web_search")
        assert tool["filters"] == {
            "allowed_domains": ["weather.example"],
            "blocked_domains": ["ads.weather.example"],
        }
        assert tool["user_location"] == {
            "type": "approximate",
            "country": "US",
            "region": "Illinois",
            "city": "Chicago",
            "timezone": "America/Chicago",
        }
        assert "web_search_call.action.sources" in first["include"]
        assert all(tool["type"] != "web_search" for tool in third["tools"])
        assert "web_search_call.action.sources" not in third["include"]
        assert any(
            item.get("role") == "user"
            and {"type": "input_text", "text": "Remember Ada."} in item["content"]
            for item in third["input"]
        )


@pytest.mark.parametrize("with_evidence", [True, False], ids=["with-evidence", "without-evidence"])
async def test_hosted_search_evidence_reaches_history_events_and_next_request(with_evidence):
    async with timeout(30), search_peer() as (model, searches):

        async def omit_usage(event):
            if event["type"] == "response.completed":
                event["response"].pop("usage")
                event["response"].pop("usage_metadata")

        if not with_evidence:
            model.before_event = omit_usage
        action = {"type": "search", "query": "Chicago weather"}
        parts = [
            {"type": "output_text", "text": "Chicago forecast. "},
            {"type": "output_text", "text": "Clear skies."},
        ]
        if with_evidence:
            action["sources"] = [
                {"type": "url", "url": "https://weather.example/chicago"},
                {"type": "url", "url": "https://weather.example/forecast"},
            ]
            hidden = "<oai-mem-citation>fixture marker</oai-mem-citation>"
            parts[1]["text"] = hidden + parts[1]["text"]
            parts[1]["annotations"] = [
                {
                    "type": "url_citation",
                    "url": "https://weather.example/chicago",
                    "title": "Chicago forecast",
                    "start_index": len(hidden),
                    "end_index": len(hidden) + 11,
                },
                {
                    "type": "file_citation",
                    "file_id": "file_fixture",
                    "filename": "fixture.txt",
                    "index": 0,
                },
            ]
        model.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "web_search_call",
                    "id": "ws_chicago",
                    "status": "completed",
                    "action": action,
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_chicago",
                    "role": "assistant",
                    "content": parts,
                },
            },
        )
        provider = NativeProvider(base_url=model.url, api_key="hosted-evidence-fixture")
        agent = Agent(
            instructions="Research the weather.",
            model="gpt-5.5",
            provider=provider,
            model_settings={"tool_choice": "required"},
            tools=[WebSearch(route="hosted")],
        )
        async with agent.session() as session:
            turn = await session.submit("Find the Chicago forecast.")
            events = [event async for event in turn.events()]
            settled = await turn.get_result()
            assert settled.final_output == "Chicago forecast. Clear skies."
            items = [item async for item in session.items()]
            message = next(
                item for item in items if item["type"] == "message" and item["role"] == "assistant"
            )
            search = next(item for item in items if item["type"] == "web_search_call")
            assert message["content"] == parts
            assert search["action"] == {**action, "queries": None}

            observed = {
                event.item["type"]: event.item
                for event in events
                if isinstance(event, ModelOutputItemCompleted)
            }
            assert observed["web_search_call"] == search
            if with_evidence:
                cited_part = message["content"][1]
                citation = cited_part["annotations"][0]
                start, end = citation["start_index"], citation["end_index"]
                assert 0 <= start < end <= len(cited_part["text"])
                assert cited_part["text"][start:end] == "Clear skies"
                assert observed["message"]["content"] == parts
                assert observed["message"]["display_text"] == settled.final_output
                assert settled.thread_usage is not None
                assert settled.thread_usage.last_token_usage.total_tokens == 14
            else:
                assert "message" not in observed
                assert settled.thread_usage is None
            recorded = [turn async for turn in session.turns()]
            received = recorded[0]["responses"][0]
            assert received["usage_source"] == "received"
            assert (
                received["usage"]["total_tokens"] if received["usage"] is not None else None
            ) == (14 if with_evidence else None)

            state = await session.save_state()

        model.respond(assistant_output("Reused the prior evidence."))
        async with agent.session(state=state) as restored:
            assert [
                item async for item in restored.items() if item["type"] == "web_search_call"
            ] == [search]
            await restored.update(model_settings={"tool_choice": "auto"})
            followup = await restored.submit("Repeat what the source said.")
            assert (await followup.get_result()).final_output == "Reused the prior evidence."

        assert not searches
        original, followup_request = model.requests
        assert original["tool_choice"] == "required"
        assert followup_request["tool_choice"] == "auto"
        replayed = {item["type"]: item for item in followup_request["input"]}
        message = next(
            item for item in followup_request["input"] if item.get("role") == "assistant"
        )
        assert message["content"] == parts
        assert replayed["web_search_call"]["action"].get("sources") == action.get("sources")


async def test_unknown_annotation_format_does_not_discard_assistant_message():
    async with timeout(20), search_peer() as (model, searches):
        reply = assistant_output("Answer with unfamiliar metadata.")
        part = reply["item"]["content"][0]
        part["annotations"] = {"unknown_provider_format": ["kept"]}
        model.respond(reply)
        provider = NativeProvider(base_url=model.url, api_key="unknown-annotation-fixture")
        async with (
            provider,
            Agent(instructions="Answer.", model="gpt-5.5", provider=provider).session() as session,
        ):
            turn = await session.submit("Answer the question.")
            assert (await turn.get_result()).final_output == part["text"]
            assert [
                item["content"]
                async for item in session.items()
                if item["type"] == "message" and item["role"] == "assistant"
            ] == [[part]]
        assert len(model.requests) == 1
        assert not searches


@pytest.mark.parametrize(
    "search",
    [
        WebSearch(allowed_domains=["weather.example"]),
        WebSearch(blocked_domains=["weather.example"]),
        WebSearch(location={"timezone": "America/Chicago"}),
    ],
)
async def test_standalone_controls_rejected_before_provider_io(search):
    async with timeout(10), search_peer() as (model, searches):
        provider = NativeProvider(base_url=model.url, api_key="standalone-controls-fixture")
        agent = Agent(instructions="Answer.", provider=provider, tools=[search])
        with pytest.raises(ValueError, match="require route='hosted'"):
            agent.validate()
        with pytest.raises(ValueError, match="require route='hosted'"):
            async with agent.session():
                pytest.fail("Standalone search accepted hosted controls")
        assert not model.requests and not searches


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
        # The source has no Environment; keep this model-admission fixture in legacy mode.
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
            )
            assert executor.attempts == 0
            if admitted:
                model.respond(assistant_output("resumed receipt"))
                async with destination.session(
                    storage=storage,
                    resume=identity,
                    environment=declaration(executor, write_policy="legacy"),
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
                        storage=storage,
                        resume=identity,
                        environment=declaration(executor, write_policy="legacy"),
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
        # The source has no Environment; keep this model-admission fixture in legacy mode.
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
                async with restored.session(
                    state=state, environment=declaration(executor, write_policy="legacy")
                ):
                    assert executor.attempts > 0
            else:
                with pytest.raises((ValueError, RuntimeError), match="hosted"):
                    async with restored.session(
                        state=state, environment=declaration(executor, write_policy="legacy")
                    ):
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
        # The source has no Environment; keep this model-admission fixture in legacy mode.
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
                async with restored.session(
                    state=state, environment=declaration(executor, write_policy="legacy")
                ):
                    assert executor.attempts > 0
            else:
                with pytest.raises((ValueError, RuntimeError)):
                    async with restored.session(
                        state=state, environment=declaration(executor, write_policy="legacy")
                    ):
                        pytest.fail("Invalid native data must not admit a connection")
                assert executor.attempts == 0 and not executor.requests
                # A rejected restore did not create or reserve the portable root.
                async with restored.session(
                    state=good, environment=declaration(executor, write_policy="legacy")
                ):
                    assert executor.attempts > 0
            assert len(model.requests) == count and not searches
