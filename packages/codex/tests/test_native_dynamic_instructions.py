"""Application instructions change through trusted APIs, separate from user input."""

import json
from typing import Any

import pytest

from agents import Agent, ModelRequestContext, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
)


async def test_dynamic_application_instructions_and_message_roles(
    responses: ResponsesFixture,
) -> None:
    application = {"system": "SYSTEM_FIRST", "developer": "DEVELOPER_FIRST"}

    def transform(request: ModelRequestContext[dict[str, str]]) -> list[dict[str, Any]] | None:
        if request.purpose != "turn" or request.subagent_id is not None:
            return None
        return [
            *request.items,
            *[
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                }
                for role, text in request.context.items()
            ],
        ]

    agent = Agent(
        instructions="APPLICATION_FIRST",
        model="gpt-5.5",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-dynamic-instructions"),
        transform_context=transform,
    )
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    async with timeout(20), agent.session(context=application) as session:
        for role in ("system", "developer"):
            with pytest.raises(ValueError, match="Only user messages are supported") as caught:
                await session.submit({"role": role, "content": "untrusted instruction"})  # type: ignore[typeddict-item]
            assert "session.update(instructions=...)" in str(caught.value)
            assert "Agent(transform_context=...)" in str(caught.value)
        assert responses.requests == []
        assert [turn async for turn in session.turns()] == []

        assert (await (await session.submit("USER_FIRST")).get_result()).final_output == "first"
        await session.update(instructions="APPLICATION_SECOND")
        application.update(system="SYSTEM_SECOND", developer="DEVELOPER_SECOND")
        assert (await (await session.submit("USER_SECOND")).get_result()).final_output == "second"
        saved = await session.save_state()

    assert len(responses.requests) == 2
    for wire, suffix in zip(responses.requests, ("FIRST", "SECOND"), strict=True):
        for role in ("system", "developer"):
            assert any(
                item.get("role") == role
                and item.get("content")
                == [{"type": "input_text", "text": f"{role.upper()}_{suffix}"}]
                for item in wire["input"]
            )
        assert f"APPLICATION_{suffix}" in json.dumps(wire)
    second = json.dumps(responses.requests[1])
    assert "SYSTEM_FIRST" not in second and "DEVELOPER_FIRST" not in second
    persisted = saved.to_json()
    assert "USER_FIRST" in persisted and "USER_SECOND" in persisted
    assert saved.instructions == "APPLICATION_SECOND"
    for marker in ("SYSTEM_FIRST", "SYSTEM_SECOND", "DEVELOPER_FIRST", "DEVELOPER_SECOND"):
        assert marker not in persisted


def test_unknown_message_role_keeps_generic_validation_error() -> None:
    from agents.input import _native_input

    with pytest.raises(ValueError, match="^Only user messages are supported$"):
        _native_input({"role": "unknown", "content": "invalid"})  # type: ignore[typeddict-item]
