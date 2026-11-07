"""Hosted tools retain Session ownership across native startup and delivery optimizations."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
from typing import Any

import pytest

from agents import (
    Agent,
    CodeInterpreter,
    FileSearch,
    ImageGeneration,
    NativeError,
    NativeProvider,
    TurnStoppedError,
)
from agents.observation import HostedToolCallProgress, ModelOutputItemCompleted, TurnFinished
from agents.tools import (
    CodeInterpreterAutoContainer,
    FileSearchComparisonFilter,
    ImageGenerationMask,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_hosted_tools import PNG, completed, provider
from tests.test_sdk_session_transport import disconnected, http_peer


async def test_hosted_capture_before_open_and_fresh_session_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Match the no-CA HTTP pool test without changing this module's other tests.
    for name in (
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "OPENAI_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")
    native = importlib.import_module("agents._native")
    create_engine = native.create_engine
    creating, release = asyncio.Event(), asyncio.Event()

    async def held_create(*args: Any, **kwargs: Any) -> Any:
        creating.set()
        await release.wait()
        return await create_engine(*args, **kwargs)

    # Hold acquisition, but leave native startup, HTTP and history unchanged.
    monkeypatch.setattr(native, "create_engine", held_create)
    vector_ids = ["vs_captured"]
    file_ids = ["file_captured"]
    filters: FileSearchComparisonFilter = {"type": "eq", "key": "team", "value": "original"}
    container: CodeInterpreterAutoContainer = {"type": "auto", "file_ids": file_ids}
    mask: ImageGenerationMask = {"image_url": PNG}
    raw = {
        "type": "file_search_call",
        "id": "fs_first_session",
        "status": "completed",
        "queries": ["first private query"],
        "results": [],
    }
    async with timeout(40), http_peer() as peer:
        peer.respond(completed(raw), assistant_output("first complete"))
        peer.respond(assistant_output("fresh complete"))
        borrowed = NativeProvider(
            base_url=peer.url, api_key="synthetic-no-ca-hosted-key", supports_websockets=False
        )
        agent = Agent(
            instructions="Use the hosted tools.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[
                FileSearch(
                    vector_store_ids=vector_ids, filters=filters, include_search_results=True
                ),
                CodeInterpreter(container=container),
                ImageGeneration(action="edit", input_image_mask=mask),
            ],
        )
        async with borrowed:
            opening = asyncio.create_task(
                agent.run(
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "first private input"},
                            {"type": "input_image", "image_url": PNG},
                        ],
                    }
                )
            )
            try:
                await creating.wait()
                assert peer.requests == []
                vector_ids.append("vs_late")
                file_ids.append("file_late")
                filters["value"] = "late"
                mask.clear()
                release.set()
                assert (await opening).final_output == "first complete"
            finally:
                release.set()
                await asyncio.gather(opening, return_exceptions=True)

            fresh = Agent(instructions="Answer directly.", model="gpt-5.2", provider=borrowed)
            assert (await fresh.run("second private input")).final_output == "fresh complete"
            assert len(peer.transports) == 2
            if sys.platform == "linux":
                assert peer.transports[0] is peer.transports[1]
                assert not peer.transports[0].is_closing()
        # Observe Provider cleanup before TestServer can close its sockets.
        await disconnected(peer.transports)

    assert len(peer.requests) == 2
    first, second = peer.requests
    hosted_types = {"file_search", "code_interpreter", "image_generation"}
    assert {tool["type"]: tool for tool in first["tools"] if tool["type"] in hosted_types} == {
        "file_search": {
            "type": "file_search",
            "vector_store_ids": ["vs_captured"],
            "filters": {"type": "eq", "key": "team", "value": "original"},
        },
        "code_interpreter": {
            "type": "code_interpreter",
            "container": {"type": "auto", "file_ids": ["file_captured"]},
        },
        "image_generation": {
            "type": "image_generation",
            "action": "edit",
            "input_image_mask": {"image_url": PNG},
        },
    }
    assert "file_search_call.results" in first["include"]
    assert not [tool for tool in second.get("tools", []) if tool["type"] in hosted_types]
    assert "file_search_call.results" not in second.get("include", [])
    assert "first private input" not in json.dumps(second["input"])
    assert not [item for item in second["input"] if item.get("type") == raw["type"]]


async def test_hosted_progress_cancel_preserves_sibling_and_session_recovery(
    responses: ResponsesFixture,
) -> None:
    first_blocked, sibling_blocked = asyncio.Event(), asyncio.Event()
    first_release, sibling_release = asyncio.Event(), asyncio.Event()
    preview = base64.b64encode(b"synthetic-preview" * 8192).decode("ascii")
    assert len(preview) > 65_536
    progress = {
        "type": "response.image_generation_call.partial_image",
        "item_id": "ig_cancelled",
        "output_index": 0,
        "sequence_number": 1,
        "partial_image_index": 0,
        "partial_image_b64": preview,
    }
    cancelled_image = {
        "type": "image_generation_call",
        "id": "ig_cancelled",
        "status": "completed",
        "result": "Y2FuY2VsbGVk",
    }
    sibling_raw = {
        "type": "file_search_call",
        "id": "fs_sibling",
        "status": "completed",
        "queries": ["sibling query"],
        "results": [],
    }

    async def hold_output(event: dict[str, Any]) -> None:
        if event["type"] != "response.output_item.done":
            return
        if event["item"]["id"] == cancelled_image["id"]:
            first_blocked.set()
            await first_release.wait()
        elif event["item"]["id"] == sibling_raw["id"]:
            sibling_blocked.set()
            await sibling_release.wait()

    responses.before_event = hold_output
    borrowed = provider(responses)
    image_agent = Agent(
        instructions="Generate an image.",
        model="gpt-5.2",
        provider=borrowed,
        tools=[ImageGeneration(partial_images=1)],
    )
    search_agent = Agent(
        instructions="Search the sibling store.",
        model="gpt-5.2",
        provider=borrowed,
        tools=[FileSearch(vector_store_ids=["vs_sibling"])],
    )
    async with (
        timeout(30),
        borrowed,
        image_agent.session() as first,
        search_agent.session() as sibling,
    ):
        try:
            responses.respond(progress, completed(cancelled_image))
            turn = await first.submit("first private image prompt")
            await first_blocked.wait()
            async for event in turn.events(include_execution_details=True):
                if isinstance(event, HostedToolCallProgress):
                    assert event.event == progress
                    assert event.session_id == first.id
                    assert event.turn_id == turn.turn_id
                    assert event.submission_id == turn.submission_id
                    break
            else:
                pytest.fail("Image preview did not reach the caller")

            responses.respond(completed(sibling_raw))
            other_turn = await sibling.submit("sibling private query")
            await sibling_blocked.wait()
            await turn.cancel()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            events = [event async for event in turn.events()]
            assert [event.status for event in events if isinstance(event, TurnFinished)] == [
                "cancelled"
            ]
            assert not [
                item async for item in first.items() if item["type"] == "image_generation_call"
            ]

            sibling_release.set()
            assert (await other_turn.get_result()).final_output == ""
            other_events = [event async for event in other_turn.events()]
            assert [event.status for event in other_events if isinstance(event, TurnFinished)] == [
                "completed"
            ]
            assert not [
                event for event in other_events if isinstance(event, HostedToolCallProgress)
            ]
            calls = [item async for item in sibling.items() if item["type"] == "file_search_call"]
            assert len(calls) == 1
            assert calls[0]["provider_data"] == sibling_raw
            assert calls[0]["turn_id"] == other_turn.turn_id

            recovered_raw = {**cancelled_image, "id": "ig_recovered", "result": "cmVjb3ZlcmVk"}
            responses.respond(completed(recovered_raw))
            recovered = await first.submit("recover the original session")
            assert (await recovered.get_result()).final_output == ""
            recovered_events = [event async for event in recovered.events()]
            assert [
                event.status for event in recovered_events if isinstance(event, TurnFinished)
            ] == ["completed"]
            images = [
                item async for item in first.items() if item["type"] == "image_generation_call"
            ]
            assert len(images) == 1
            assert images[0]["provider_data"] == recovered_raw
            assert images[0]["turn_id"] == recovered.turn_id
        finally:
            first_release.set()
            sibling_release.set()

    assert len(responses.requests) == 3
    assert "first private image prompt" not in json.dumps(responses.requests[1]["input"])
    for request in responses.requests:
        assert not [item for item in request["input"] if item.get("type") == "function_call_output"]


async def test_completed_hosted_item_survives_provider_failure_without_success(
    responses: ResponsesFixture,
) -> None:
    raw = {
        "type": "image_generation_call",
        "id": "ig_before_failure",
        "status": "completed",
        "result": "ZmluYWwtaW1hZ2U=",
        "revised_prompt": None,
        "future_field": {"retained": True},
    }
    responses.respond(
        completed(raw),
        {
            "type": "response.failed",
            "response": {
                "id": "resp_1",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "synthetic hosted request rejected",
                },
            },
        },
    )
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Generate an image.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[ImageGeneration()],
        ).session() as session,
    ):
        turn = await session.submit("Generate before the provider failure.")
        with pytest.raises(NativeError, match="synthetic hosted request rejected"):
            await turn.get_result()
        events = [event async for event in turn.events(include_execution_details=True)]
        assert [event.status for event in events if isinstance(event, TurnFinished)] == ["failed"]
        images = [item async for item in session.items() if item["type"] == "image_generation_call"]
        assert len(images) == 1
        assert images[0]["id"] == raw["id"]
        assert images[0]["result"] == raw["result"]
        assert images[0]["provider_data"] == raw
        assert images[0]["turn_id"] == turn.turn_id
        assert [
            event.item
            for event in events
            if isinstance(event, ModelOutputItemCompleted)
            and event.item["type"] == "image_generation_call"
        ] == images
        turns = [record async for record in session.turns()]
        assert len(turns) == 1
        assert turns[0]["status"] == "failed"
        assert len(turns[0]["responses"]) == 1
        assert turns[0]["responses"][0]["status"] == "failed"
        assert turns[0]["responses"][0]["response_id"] == "resp_1"
    assert len(responses.requests) == 1  # No fabricated local result or continuation.
    assert not [
        item
        for item in responses.requests[0]["input"]
        if item.get("type") == "function_call_output"
    ]
