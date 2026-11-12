"""HTTP retries retain the public Turn's sampling-stop authority."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, TurnStoppedError
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output


@pytest.mark.parametrize("cancel", [False, True])
async def test_http_retry_checks_stop_before_a_fresh_attempt(cancel: bool) -> None:
    entered, release_response, interrupt_entered, release_interrupt, retried = (
        asyncio.Event() for _ in range(5)
    )
    attempts = 0
    peer = ResponsesFixture()
    peer.respond(assistant_output("retry recovered"))

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await request.read()
            entered.set()
            await release_response.wait()
            return web.json_response({"error": {"message": "retryable failure"}}, status=503)
        retried.set()
        return await peer.handle(request)

    class GatedInterrupt:
        def __init__(self, native):
            self.native = native

        def __getattr__(self, name):
            return getattr(self.native, name)

        async def _interrupt_session(self, *args, **kwargs):
            interrupt_entered.set()
            if cancel:
                await release_interrupt.wait()
            return await self.native._interrupt_session(*args, **kwargs)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(15), TestServer(app) as server:
        provider = NativeProvider(base_url=str(server.make_url("/v1")), api_key="synthetic-key")
        async with (
            provider,
            Agent(
                model="gpt-5.5", instructions="Answer briefly.", tools=[], provider=provider
            ).session() as session,
        ):
            session._native = GatedInterrupt(session._native)
            turn = await session.submit("Return a short answer.")
            await entered.wait()
            if not cancel:
                release_response.set()
                assert (await turn.get_result()).final_output == "retry recovered"
                assert attempts == 2
                return
            # Turn.cancel reserves the exact native sampling stop before this
            # wrapper delays only its asynchronous interruption/cleanup.
            stopping = asyncio.create_task(turn.cancel())
            try:
                await interrupt_entered.wait()
                release_response.set()
                with pytest.raises(asyncio.TimeoutError):
                    async with timeout(2):
                        await retried.wait()
                assert attempts == 1
                assert not stopping.done()
            finally:
                release_response.set()
                release_interrupt.set()
                await stopping
            assert attempts == 1
            with pytest.raises(TurnStoppedError):
                await turn.get_result()


@pytest.mark.parametrize("budget", [0, 1])
async def test_provider_request_retry_budget_controls_actual_attempts(budget: int) -> None:
    from agents import NativeError

    attempts = 0
    peer = ResponsesFixture()
    peer.respond(assistant_output("retry recovered"))

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await request.read()
            return web.json_response({"error": {"message": "retryable failure"}}, status=503)
        return await peer.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(15), TestServer(app) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-key",
            supports_websockets=False,
            request_max_retries=budget,
            stream_max_retries=0,
        ) as provider:
            agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
            if budget:
                assert (await agent.run("Reply.")).final_output == "retry recovered"
            else:
                with pytest.raises(NativeError):
                    await agent.run("Reply.")
    assert attempts == budget + 1


@pytest.mark.parametrize("with_hosted_shell", [False, True])
async def test_affirmative_policy_cannot_replay_hosted_shell_after_disconnect(
    with_hosted_shell: bool,
) -> None:
    from agents import HostedShell, ModelRetrySettings, NativeError, RetryDecision

    requests = []
    peer = ResponsesFixture()
    peer.respond(assistant_output("retry recovered"))

    async def handle(request: web.Request) -> web.StreamResponse:
        requests.append(await request.json())
        if len(requests) == 1:
            transport = request.transport
            assert transport is not None
            transport.close()
            return web.Response()
        return await peer.handle(request)

    def retry(_context) -> RetryDecision:
        return RetryDecision(True, delay_ms=0)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(15), TestServer(app) as server:
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-shell-retry",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        )
        tools = (
            [HostedShell(environment={"type": "container_reference", "container_id": "cntr_test"})]
            if with_hosted_shell
            else []
        )
        async with provider:
            agent = Agent(
                model="gpt-5.5",
                instructions="Answer.",
                tools=tools,
                provider=provider,
                model_settings={"retry": ModelRetrySettings(max_retries=1, policy=retry)},
            )
            if with_hosted_shell:
                with pytest.raises(NativeError):
                    await agent.run("Reply.")
            else:
                assert (await agent.run("Reply.")).final_output == "retry recovered"

    assert len(requests) == (1 if with_hosted_shell else 2)
    provider_tools = requests[0].get("tools", [])
    assert any(tool["type"] == "shell" for tool in provider_tools) is with_hosted_shell
