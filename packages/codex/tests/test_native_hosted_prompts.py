"""Prompt-owned defaults through the public native request/callback owner."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, ModelRequest, ModelResponse, NativeProvider
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_prompt_defaults_and_explicit_empty_overrides(
    responses: ResponsesFixture, explicit: bool
) -> None:
    responses.respond(assistant_output("prompt result"))
    reference = {"id": "pmpt_owned", "variables": {"topic": "captured"}}
    resolutions = 0

    def source(context: Any) -> Any:
        nonlocal resolutions
        resolutions += 1
        return reference

    def transform(context: Any) -> None:
        reference["variables"]["topic"] = "changed after capture"  # type: ignore[index]

    options: dict[str, Any] = (
        {"model": "fixture-explicit", "instructions": "", "tools": []} if explicit else {}
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(
            provider=provider, hosted_prompt=source, transform_context=transform, **options
        )
        assert (await agent.run("Use current declared prompt.")).final_output == "prompt result"
    assert resolutions == 1
    assert len(responses.requests) == 1
    request = responses.requests[0]
    assert request["prompt"] == {"id": "pmpt_owned", "variables": {"topic": "captured"}}
    if explicit:
        assert request["model"] == "fixture-explicit"
        assert request["instructions"] == ""
        assert request["tools"] == []
    else:
        for field in (
            "model",
            "instructions",
            "tools",
            "reasoning",
            "text",
            "tool_choice",
            "parallel_tool_calls",
        ):
            assert field not in request


async def test_native_prompt_cold_restore_uses_current_declaration(
    responses: ResponsesFixture,
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    current = "pmpt_before"

    def source(context: Any) -> Any:
        return {"id": current}

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(hosted_prompt=source, provider=provider)
        async with agent.session() as session:
            await (await session.submit("before restore")).get_result()
            saved = await session.save_state()
        current = "pmpt_current"
        async with agent.session(state=saved) as restored:
            await (await restored.submit("after restore")).get_result()
    assert [request["prompt"]["id"] for request in responses.requests] == [
        "pmpt_before",
        "pmpt_current",
    ]
    assert all("instructions" not in request for request in responses.requests)


async def test_native_prompt_model_update_does_not_inject_default_instructions(
    responses: ResponsesFixture,
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(hosted_prompt={"id": "pmpt_current"}, provider=provider)
        async with agent.session() as session:
            await (await session.submit("before model update")).get_result()
            await session.update(model="fixture-updated")
            await (await session.submit("after model update")).get_result()
    assert len(responses.requests) == 2
    assert responses.requests[1]["model"] == "fixture-updated"
    assert "instructions" not in responses.requests[1]
    assert "model_switch" not in str(responses.requests[1]["input"])


@pytest.mark.parametrize("instructions", ["", "Use the explicit instruction update."])
async def test_native_prompt_explicit_instruction_update_overrides_omission(
    responses: ResponsesFixture, instructions: str
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(hosted_prompt={"id": "pmpt_current"}, provider=provider)
        async with agent.session() as session:
            await (await session.submit("before instruction update")).get_result()
            await session.update(instructions=instructions)
            await (await session.submit("after instruction update")).get_result()
    assert len(responses.requests) == 2
    assert "instructions" not in responses.requests[0]
    assert responses.requests[1]["instructions"] == instructions


async def test_native_prompt_resolver_receives_routing_hint_without_wire_model(
    responses: ResponsesFixture,
) -> None:
    hints: list[str] = []
    bodies: list[dict[str, Any]] = []

    class Backend:
        def complete(self, request: ModelRequest) -> ModelResponse:
            bodies.append(dict(request.body))
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "resolved prompt"}],
                    }
                ]
            )

    class Resolver:
        def get_model(self, model: str) -> Backend:
            hints.append(model)
            return Backend()

    async with NativeProvider(
        base_url=responses.url,
        model_backend=Resolver(),
        supports_websockets=False,
    ) as provider:
        result = await Agent(hosted_prompt={"id": "pmpt_resolved"}, provider=provider).run("answer")
    assert result.final_output == "resolved prompt"
    assert len(hints) == 1 and isinstance(hints[0], str) and hints[0]
    assert len(bodies) == 1 and "model" not in bodies[0]
    assert bodies[0]["prompt"] == {"id": "pmpt_resolved"}
    assert responses.requests == []
