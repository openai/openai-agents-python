from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from openai import AsyncOpenAI

from agents import Agent, NativeError, ResponsesCompaction
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider


@asynccontextmanager
async def _matching_compaction_client(responses, compact, **client_options):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def handle(request):
        result = await compact(**await request.json())
        return web.json_response(
            {"id": "cmp_matching", "object": "response.compaction", "created_at": 0, **result}
        )

    app = web.Application()
    app.router.add_post("/v1/responses", responses.handle)
    app.router.add_post("/v1/responses/compact", handle)
    async with TestServer(app) as server:
        responses.url = str(server.make_url("/v1"))
        async with AsyncOpenAI(
            **{
                "api_key": "synthetic-native-agent-key",
                "base_url": responses.url,
                "max_retries": 0,
                **client_options,
            }
        ) as client:
            yield client


@pytest.mark.parametrize(
    ("mode", "store", "automatic", "transformed"),
    [
        ("auto", True, False, False),
        ("previous_response_id", True, False, False),
        ("auto", False, False, False),
        ("previous_response_id", False, False, False),
        ("auto", True, True, False),
        ("auto", True, False, True),
        ("previous_response_id", True, False, True),
    ],
)
async def test_compaction_uses_only_a_stored_acknowledged_native_window(
    responses, mode, store, automatic, transformed
):
    calls = []

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "COMPACT_WINDOW"}]}

    def transform(context):
        return [item for item in context.items if item.get("role") != "developer"]

    async with _matching_compaction_client(responses, compact) as client:
        responses.respond(assistant_output("ACKNOWLEDGED_WINDOW"))
        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": store},
            instructions="Current application instructions.",
            transform_context=transform if transformed else None,
            compaction=ResponsesCompaction(
                client=client,
                model="gpt-5.5",
                mode=mode,
                should_compact=lambda context: automatic,
            ),
        )
        async with timeout(30), agent.session() as session:
            await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
            assert responses.requests[0]["store"] is store
            if (not store or transformed) and mode == "previous_response_id":
                with pytest.raises(ValueError, match="acknowledged"):
                    await session.compact()
                assert not calls
                return
            if not automatic:
                await session.compact()
            assert len(calls) == 1
            if store and not transformed:
                assert calls[0]["previous_response_id"] == "resp_1"
                assert "input" not in calls[0]
            else:
                assert "previous_response_id" not in calls[0]
                assert "ACKNOWLEDGED_WINDOW" in json.dumps(calls[0]["input"])


@pytest.mark.parametrize(
    ("mode", "automatic"),
    [("auto", False), ("auto", True), ("previous_response_id", False)],
)
async def test_compaction_only_transform_precedes_lineage_selection(responses, mode, automatic):
    calls = []
    policy_modes = []
    compaction_transforms = 0

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "COMPACT_WINDOW"}]}

    def transform(context):
        nonlocal compaction_transforms
        if context.purpose != "compaction":
            return None
        compaction_transforms += 1
        return [item for item in context.items if "PRIVATE_FOR_COMPACTION" not in json.dumps(item)]

    def should_compact(context):
        policy_modes.append(context.mode)
        return automatic

    async with _matching_compaction_client(responses, compact) as client:
        responses.respond(assistant_output("PRIVATE_FOR_COMPACTION"))
        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            instructions="Current application instructions.",
            transform_context=transform,
            compaction=ResponsesCompaction(
                client=client,
                model="gpt-5.5",
                mode=mode,
                should_compact=should_compact,
            ),
        )
        async with timeout(30), agent.session() as session:
            await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
            if mode == "previous_response_id":
                with pytest.raises(ValueError, match="acknowledged"):
                    await session.compact()
                assert not calls
                assert compaction_transforms == 0
                return
            if not automatic:
                await session.compact()
            assert len(calls) == 1
            assert policy_modes == ["input"]
            assert compaction_transforms == 1
            assert "previous_response_id" not in calls[0]
            assert "EXACT_NATIVE_INPUT" in json.dumps(calls[0]["input"])
            assert "PRIVATE_FOR_COMPACTION" not in json.dumps(calls[0]["input"])


@pytest.mark.parametrize("rebound", [False, True])
async def test_auto_compaction_keeps_input_for_a_different_borrowed_endpoint(responses, rebound):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    calls = []

    async def compact(request):
        body = await request.json()
        calls.append(body)
        if "previous_response_id" in body:
            return web.json_response(
                {
                    "error": {
                        "message": "Unknown response in this account",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        return web.json_response(
            {
                "id": "cmp_other",
                "object": "response.compaction",
                "created_at": 0,
                "output": [{"type": "compaction", "encrypted_content": "OTHER_ENDPOINT_WINDOW"}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses/compact", compact)
    responses.respond(assistant_output("ACKNOWLEDGED_NATIVE_ENDPOINT"))
    async with TestServer(app) as server:
        async with (
            AsyncOpenAI(
                api_key="synthetic-native-agent-key",
                base_url=str(server.make_url("/v1")),
                max_retries=0,
            ) as foreign,
            AsyncOpenAI(
                api_key="synthetic-native-agent-key",
                base_url=responses.url,
                max_retries=0,
            ) as native_client,
        ):
            client = native_client if rebound else foreign
            if rebound:
                client.responses = foreign.responses
            agent = Agent(
                provider=provider(responses),
                model="gpt-5.5",
                model_settings={"store": True},
                instructions="Current application instructions.",
                compaction=ResponsesCompaction(client=client, model="gpt-5.5"),
            )
            async with timeout(30), agent.session() as session:
                await (await session.submit("INPUT_MUST_REACH_BORROWED_ENDPOINT")).get_result()
                before = (await session.history_snapshot()).items
                with pytest.raises(
                    NativeError, match="new or modified instruction or control item"
                ):
                    await session.compact()
                assert (await session.history_snapshot()).items == before
    assert len(calls) == 1
    assert "previous_response_id" not in calls[0]
    assert "ACKNOWLEDGED_NATIVE_ENDPOINT" in json.dumps(calls[0]["input"])
    assert "INPUT_MUST_REACH_BORROWED_ENDPOINT" in json.dumps(calls[0]["input"])


@pytest.mark.parametrize(
    "client_options",
    [
        {"api_key": "synthetic-other-key"},
        {"project": "other-project"},
        {"organization": "other-organization"},
        {"default_headers": {"x-account-routing": "other"}},
        {"default_query": {"account": "other"}},
    ],
)
@pytest.mark.parametrize("mode", ["auto", "previous_response_id"])
async def test_compaction_rejects_unproven_borrowed_account_scope(responses, client_options, mode):
    calls = []
    policy_modes = []

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "BORROWED_WINDOW"}]}

    def should_compact(context):
        policy_modes.append(context.mode)
        return False

    responses.respond(assistant_output("ACKNOWLEDGED_ACCOUNT_WINDOW"))
    async with _matching_compaction_client(responses, compact, **client_options) as client:
        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            instructions="Current application instructions.",
            compaction=ResponsesCompaction(
                client=client, model="gpt-5.5", mode=mode, should_compact=should_compact
            ),
        )
        async with timeout(30), agent.session() as session:
            await (await session.submit("EXACT_ACCOUNT_INPUT")).get_result()
            if mode == "previous_response_id":
                with pytest.raises(ValueError, match="acknowledged"):
                    await session.compact()
                assert not calls
                assert not policy_modes
            else:
                before = (await session.history_snapshot()).items
                with pytest.raises(
                    NativeError, match="new or modified instruction or control item"
                ):
                    await session.compact()
                assert (await session.history_snapshot()).items == before
                assert policy_modes == ["input"]
                assert len(calls) == 1
                assert "previous_response_id" not in calls[0]
                assert "ACKNOWLEDGED_ACCOUNT_WINDOW" in json.dumps(calls[0]["input"])


async def test_compaction_matches_explicit_native_organization_and_project(responses):
    from agents import NativeProvider

    calls = []

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "SCOPED_WINDOW"}]}

    responses.respond(assistant_output("SCOPED_ACCOUNT_WINDOW"))
    async with _matching_compaction_client(
        responses, compact, organization="native-org", project="native-project"
    ) as client:
        connection = NativeProvider(
            base_url=responses.url,
            api_key="synthetic-native-agent-key",
            organization="native-org",
            project="native-project",
        )
        async with connection:
            agent = Agent(
                provider=connection,
                model="gpt-5.5",
                model_settings={"store": True},
                instructions="Current application instructions.",
                compaction=ResponsesCompaction(
                    client=client, model="gpt-5.5", mode="previous_response_id"
                ),
            )
            async with timeout(30), agent.session() as session:
                await (await session.submit("EXACT_SCOPED_INPUT")).get_result()
                await session.compact()
    assert len(calls) == 1
    assert calls[0]["previous_response_id"] == "resp_1"
    assert "input" not in calls[0]


async def test_compaction_rechecks_client_scope_after_predicate(responses):
    calls = []

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "UNEXPECTED_WINDOW"}]}

    responses.respond(assistant_output("ORIGINAL_ANSWER_SURVIVES_CLIENT_CHANGE"))
    async with _matching_compaction_client(responses, compact) as client:

        def should_compact(context):
            assert context.mode == "previous_response_id"
            client.project = "changed-after-admission"
            return True

        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            instructions="Current application instructions.",
            compaction=ResponsesCompaction(
                client=client, model="gpt-5.5", should_compact=should_compact
            ),
        )
        async with timeout(30), agent.session() as session:
            result = await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
            assert result.final_output == "ORIGINAL_ANSWER_SURVIVES_CLIENT_CHANGE"
    assert not calls


async def test_compaction_falls_back_for_unknown_sdk_private_shape(responses, monkeypatch):
    import openai._base_client as base_client

    calls = []

    async def compact(**body):
        calls.append(body)
        return {"output": [assistant_output("INPUT_WINDOW")["item"]]}

    responses.respond(assistant_output("UNKNOWN_SDK_SHAPE_WINDOW"))
    async with _matching_compaction_client(responses, compact) as client:
        wrapper = base_client.AsyncHttpxClientWrapper
        monkeypatch.delattr(base_client, "AsyncHttpxClientWrapper")
        try:
            compaction = ResponsesCompaction(client=client, model="gpt-5.5")
        finally:
            monkeypatch.setattr(base_client, "AsyncHttpxClientWrapper", wrapper, raising=False)
        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            instructions="Current application instructions.",
            compaction=compaction,
        )
        async with timeout(30), agent.session() as session:
            await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
            await session.compact()
            assert "INPUT_WINDOW" in (await session.save_state()).to_json()
    assert len(calls) == 1
    assert "previous_response_id" not in calls[0]
    assert "UNKNOWN_SDK_SHAPE_WINDOW" in json.dumps(calls[0]["input"])


async def test_input_only_compaction_keeps_full_input_after_client_scope_rotation(responses):
    calls = []

    async def compact(**body):
        calls.append(body)
        return {"output": [assistant_output("INPUT_WINDOW")["item"]]}

    responses.respond(assistant_output("UNSTORED_NATIVE_WINDOW"))
    async with _matching_compaction_client(responses, compact) as client:
        compaction = ResponsesCompaction(client=client, model="gpt-5.5")
        client.project = "changed-without-native-response-id"
        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": False},
            instructions="Current application instructions.",
            compaction=compaction,
        )
        async with timeout(30), agent.session() as session:
            await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
            await session.compact()
            assert "INPUT_WINDOW" in (await session.save_state()).to_json()
    assert len(calls) == 1
    assert "previous_response_id" not in calls[0]
    assert "UNSTORED_NATIVE_WINDOW" in json.dumps(calls[0]["input"])


@pytest.mark.parametrize("changed", ["api_key", "project"])
@pytest.mark.parametrize("automatic", [False, True])
async def test_auto_compaction_uses_input_after_client_scope_rotation(
    responses, changed, automatic
):
    calls = []
    modes = []

    async def compact(**body):
        calls.append(body)
        return {"output": [assistant_output("ROTATED_SCOPE_WINDOW")["item"]]}

    def should_compact(context):
        modes.append(context.mode)
        return automatic

    async with _matching_compaction_client(responses, compact) as client:
        compaction = ResponsesCompaction(
            client=client, model="gpt-5.5", mode="auto", should_compact=should_compact
        )
        setattr(client, changed, "synthetic-rotated-scope")
        responses.respond(assistant_output("ACKNOWLEDGED_ROTATED_WINDOW"))
        agent = Agent(
            instructions="Current application instructions.",
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            compaction=compaction,
        )
        async with timeout(30), agent.session() as session:
            result = await (await session.submit("EXACT_ROTATED_INPUT")).get_result()
            assert result.final_output == "ACKNOWLEDGED_ROTATED_WINDOW"
            if not automatic:
                await session.compact()
            assert "ROTATED_SCOPE_WINDOW" in (await session.save_state()).to_json()
    assert len(calls) == 1
    assert modes == ["input"]
    assert "previous_response_id" not in calls[0]
    assert "EXACT_ROTATED_INPUT" in json.dumps(calls[0]["input"])
    assert "ACKNOWLEDGED_ROTATED_WINDOW" in json.dumps(calls[0]["input"])


async def test_false_predicate_keeps_a_successful_skip_after_scope_rotation(responses, monkeypatch):
    calls = []
    replies = []

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "UNEXPECTED_WINDOW"}]}

    responses.respond(assistant_output("DELIBERATE_SKIP"))
    async with _matching_compaction_client(responses, compact) as client:

        def should_compact(context):
            assert context.mode == "previous_response_id"
            client.project = "synthetic-rotated-after-selection"
            return False

        agent = Agent(
            instructions="Current application instructions.",
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            compaction=ResponsesCompaction(client=client, should_compact=should_compact),
        )
        async with timeout(30), agent.session() as session:
            native_type = type(session._native)
            original = native_type._reply_compaction_policy

            def reply(owner, step_id, value, *args, **kwargs):
                replies.append((value, kwargs.get("error", False)))
                return original(owner, step_id, value, *args, **kwargs)

            monkeypatch.setattr(native_type, "_reply_compaction_policy", reply)
            result = await (await session.submit("SKIP_AFTER_ROTATION")).get_result()
            assert result.final_output == "DELIBERATE_SKIP"
    assert replies == [(False, False)]
    assert not calls


@pytest.mark.parametrize("transport_kind", ["null", "mounted", "disguised-default"])
async def test_compaction_rejects_mounted_transport_store_identity(responses, transport_kind):
    import httpx2 as httpx
    from openai._base_client import AsyncHttpxClientWrapper

    native_calls = []
    foreign_calls = []

    async def compact(**body):
        native_calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "NATIVE_STORE"}]}

    async def foreign(request):
        request.url = httpx.URL("https://other-store.invalid/v1/responses/compact")
        request.headers["authorization"] = "Bearer synthetic-other-store"
        body = json.loads(request.content)
        assert body["previous_response_id"]
        assert "input" not in body
        foreign_calls.append(body)
        return httpx.Response(
            200,
            json={
                "id": "cmp_foreign",
                "object": "response.compaction",
                "created_at": 0,
                "output": [{"type": "compaction", "encrypted_content": "FOREIGN_STORE"}],
            },
        )

    mounted = transport_kind != "null"
    if transport_kind == "disguised-default":
        disguised = type("AsyncHTTPTransport", (httpx.MockTransport,), {"__module__": "httpx2"})
        http_client = AsyncHttpxClientWrapper(transport=disguised(foreign), trust_env=False)
    else:
        transport = httpx.MockTransport(foreign) if mounted else None
        http_client = AsyncHttpxClientWrapper(mounts={"all://": transport}, trust_env=False)
    responses.respond(assistant_output("NATIVE_ACKNOWLEDGED"))
    async with _matching_compaction_client(responses, compact, http_client=http_client) as client:
        agent = Agent(
            provider=provider(responses),
            model="gpt-5.5",
            model_settings={"store": True},
            instructions="Answer.",
            compaction=ResponsesCompaction(
                client=client,
                model="gpt-5.5",
                mode="previous_response_id",
                should_compact=lambda context: False,
            ),
        )
        async with timeout(30), agent.session() as session:
            await (await session.submit("SOURCE_STORE_INPUT")).get_result()
            if mounted:
                with pytest.raises(ValueError, match="acknowledged"):
                    await session.compact()
                assert not any(
                    row["type"] == "compacted" for row in (await session.save_state()).history
                )
            else:
                await session.compact()
    assert foreign_calls == []
    assert len(native_calls) == (0 if mounted else 1)
    if native_calls:
        assert native_calls[0]["previous_response_id"]
        assert "input" not in native_calls[0]
