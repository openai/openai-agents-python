from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from agents import Agent, HostedResponsesWebSocketModel, NativeError, NativeProvider
from agents.agent import ModelSettings
from tests._asyncio import timeout


@pytest.mark.parametrize(
    ("settings", "field"),
    [
        ({"extra_body": {"max_tool_calls": 3}}, "max_tool_calls"),
        ({"extra_body": {"multi_agent": {"enabled": False}}}, "multi_agent"),
        ({"extra_body": {"betas": ["synthetic-unsupported"]}}, "betas"),
        ({"reasoning_summary": "detailed"}, "reasoning.summary"),
    ],
)
async def test_hosted_beta_rejects_unsupported_settings_before_opening_socket(
    settings: dict[str, Any], field: str
) -> None:
    opens: list[str] = []

    async def connect(*args: Any) -> Any:
        opens.append("opened")
        raise AssertionError("Hosted beta admitted unsupported settings")

    async with (
        timeout(20),
        AsyncOpenAI(api_key="synthetic-beta") as client,
        NativeProvider(
            model_backend=HostedResponsesWebSocketModel(client, socket_factory=connect)
        ) as provider,
    ):
        agent = Agent(
            model="gpt-5.5",
            instructions="Respond briefly.",
            model_settings=cast(ModelSettings, settings),
            provider=provider,
        )
        async with agent.session() as session:
            turn = await session.submit("Hello.")
            with pytest.raises((NativeError, ValueError)) as stopped:
                await turn.get_result()
            assert field in str(stopped.value)
    assert opens == []
