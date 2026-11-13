from __future__ import annotations

from typing import Any, cast

import pytest

from agents import Agent, NativeProvider
from agents.agent import ModelSettings
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, None),
        ({"prompt_cache_ttl": "30m"}, {"ttl": "30m"}),
        ({"prompt_cache_mode": "explicit"}, {"mode": "explicit"}),
        (
            {"prompt_cache_mode": "explicit", "prompt_cache_ttl": "30m"},
            {"mode": "explicit", "ttl": "30m"},
        ),
        ({"prompt_cache_mode": None}, None),
        ({"prompt_cache_mode": None, "prompt_cache_ttl": "30m"}, {"ttl": "30m"}),
    ],
    ids=["omitted", "ttl-only", "mode-only", "mode-and-ttl", "cleared", "clear-with-ttl"],
)
async def test_native_prompt_cache_mode_wire(
    responses: ResponsesFixture, settings: dict[str, Any], expected: dict[str, str] | None
) -> None:
    responses.respond(assistant_output("Accepted."))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-cache", supports_websockets=False
    ) as provider:
        result = await Agent(
            model="gpt-5.5",
            instructions="Answer.",
            provider=provider,
            model_settings=cast(ModelSettings, settings),
        ).run("ordinary prefix")
    assert result.final_output == "Accepted."
    assert len(responses.requests) == 1
    body = responses.requests[0]
    if expected is None:
        assert "prompt_cache_options" not in body
    else:
        assert body["prompt_cache_options"] == expected
