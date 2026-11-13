"""Exercise the native compatibility retry through a real local Responses endpoint."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, HostedMCP, ModelRetrySettings, ModelSettings, NativeError, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output


@pytest.mark.parametrize(
    ("mode", "expected_requests", "succeeds"),
    [
        ("default", 2, True),
        ("unrelated_policy", 2, True),
        ("disabled", 1, False),
        ("exhausted", 4, False),
        ("ordinary_400", 1, False),
        ("hosted_tool", 1, False),
    ],
)
async def test_conversation_locked_retry_keeps_original_request(mode, expected_requests, succeeds):
    fixture = ResponsesFixture()
    fixture.respond(assistant_output("unlocked"))
    attempts = []
    policy_calls = []

    def policy(context):
        policy_calls.append(context)
        return mode in {"exhausted", "ordinary_400", "hosted_tool"}

    async def handle(request):
        attempts.append(await request.json())
        if len(attempts) == 1 or mode == "exhausted":
            code = "invalid_value" if mode == "ordinary_400" else "conversation_locked"
            return web.json_response(
                {"error": {"code": code, "message": "Synthetic rejection."}},
                status=400,
            )
        return await fixture.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    settings = {
        "default": ModelSettings(),
        "unrelated_policy": ModelSettings(retry=ModelRetrySettings(policy=policy)),
        "disabled": ModelSettings(retry=ModelRetrySettings(max_retries=0, policy=policy)),
        "exhausted": ModelSettings(retry=ModelRetrySettings(max_retries=8, policy=policy)),
        "ordinary_400": ModelSettings(retry=ModelRetrySettings(max_retries=8, policy=policy)),
        "hosted_tool": ModelSettings(retry=ModelRetrySettings(max_retries=8, policy=policy)),
    }[mode]
    async with timeout(15), TestServer(app) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-lock-test",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            agent = Agent(
                instructions="Answer.",
                provider=provider,
                model_settings=settings,
                model="gpt-5.5",
                tools=[
                    HostedMCP(
                        tool_config={
                            "type": "mcp",
                            "server_label": "remote",
                            "server_url": "https://hosted-mcp.invalid/remote",
                            "require_approval": "never",
                        }
                    )
                ]
                if mode == "hosted_tool"
                else [],
            )
            if succeeds:
                result = await agent.run("Try once.")
                assert result.final_output == "unlocked"
                assert result.run_usage.requests == expected_requests
            else:
                with pytest.raises(NativeError):
                    await agent.run("Try once.")
    assert not policy_calls
    assert len(attempts) == expected_requests
    assert all(attempt["input"] == attempts[0]["input"] for attempt in attempts)
    if mode == "hosted_tool":
        assert any(tool["type"] == "mcp" for tool in attempts[0]["tools"])
