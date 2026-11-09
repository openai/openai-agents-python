"""Preserve typed input through native provider requests and native history."""

import asyncio
import json
from typing import Any, cast

import pytest

from agents import Agent, NativeProvider, UserInput
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
)

IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-native-input-key")


def image_content(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    [message] = [
        item
        for item in items
        if item.get("role") == "user"
        and any(part.get("type") == "input_image" for part in item.get("content", []))
    ]
    return cast(list[dict[str, Any]], message["content"])


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-5.5"])
async def test_image_reference_and_order_reach_native_wire_and_history(
    responses: ResponsesFixture, model: str
) -> None:
    responses.respond(assistant_output("image accepted"))
    responses.respond(assistant_output("second turn"))
    agent = Agent(instructions="Describe the input.", model=model, provider=provider(responses))
    async with timeout(30), agent.session() as session:
        first = await session.submit(
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "before-image"},
                    {"type": "input_image", "image_url": IMAGE},
                    {"type": "input_text", "text": "after-image"},
                ],
            }
        )
        result = await (first).get_result()
        assert result.final_output == "image accepted"
        wire = image_content(responses.requests[0]["input"])
        expected: list[dict[str, Any]] = [
            {"type": "input_text", "text": "before-image"},
            {"type": "input_image", "image_url": IMAGE},
            {"type": "input_text", "text": "after-image"},
        ]
        # The pinned model catalog selects Lite for gpt-6.1-sol. Core omits
        # detail on that transport and retains its native default on gpt-5.5.
        assert responses.requests[0]["model"] == model
        if model == "gpt-5.5":
            expected[1]["detail"] = "high"
        assert wire == expected
        canonical = [dict(part) for part in expected]
        canonical[1]["detail"] = "high"
        native_history = json.loads(await session._native.read_thread_history(None))
        assert (
            image_content(
                [entry["payload"] for entry in native_history if entry["type"] == "response_item"]
            )
            == canonical
        )
        public_items = cast(list[dict[str, Any]], [item async for item in session.items()])
        assert image_content(public_items) == canonical
        second = await session.submit("retain the earlier image")
        assert (await (second).get_result()).final_output == "second turn"
        assert image_content(responses.requests[1]["input"]) == wire
        assert await (first).get_result() is result


async def test_submit_captures_the_complete_input_sequence(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(assistant_output("captured"))
    entered = asyncio.Event()
    release = asyncio.Event()
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        native = session._native

        class HeldSubmission:
            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

            async def _submit(self, request: str) -> str:
                entered.set()
                await release.wait()
                return cast(str, await native._submit(request))

        monkeypatch.setattr(session, "_native", HeldSubmission())
        parts = [{"type": "input_text", "text": "captured text"}]
        submitting = asyncio.create_task(session.submit({"role": "user", "content": parts}))
        try:
            await entered.wait()
            parts[:] = [{"type": "input_text", "text": "changed later"}]
        finally:
            release.set()
        handle = await submitting
        assert (await (handle).get_result()).final_output == "captured"
        request = json.dumps(responses.requests[0])
        assert "captured text" in request
        assert "changed later" not in request


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        b"bytes",
        [{"type": "text", "text": "raw"}],
        [{"type": "input_text", "text": "valid"}, {"type": "audio"}],
    ],
)
async def test_invalid_input_does_not_submit_a_partial_native_turn(
    responses: ResponsesFixture, invalid: Any
) -> None:
    responses.respond(assistant_output("still usable"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        with pytest.raises(TypeError, match="input"):
            await session.submit(invalid)
        assert responses.requests == []
        assert [item async for item in session.turns()] == []
        handle = await session.submit("valid input")
        assert (await (handle).get_result()).final_output == "still usable"


async def test_one_shot_accepts_typed_text(responses: ResponsesFixture) -> None:
    responses.respond(assistant_output("typed one-shot"))
    input: UserInput = {"role": "user", "content": [{"type": "input_text", "text": "typed text"}]}
    result = await Agent(instructions="Answer briefly.", provider=provider(responses)).run(input)
    assert result.final_output == "typed one-shot"
    assert any(
        item.get("role") == "user"
        and item.get("content") == [{"type": "input_text", "text": "typed text"}]
        for item in responses.requests[0]["input"]
    )


@pytest.mark.parametrize("reference", ["file-123", "image.png"])
async def test_unsupported_image_reference_fails_before_native_submission(
    responses: ResponsesFixture, reference: str
) -> None:
    responses.respond(assistant_output("still usable"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        with pytest.raises(ValueError, match="Image URL"):
            await session.submit(
                {"role": "user", "content": [{"type": "input_image", "image_url": reference}]}
            )
        assert responses.requests == []
        assert [item async for item in session.turns()] == []
        handle = await session.submit("valid input")
        assert (await (handle).get_result()).final_output == "still usable"
