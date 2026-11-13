"""Count HTTP transport entries around a borrowed Responses client.

Run `python -m examples.codex.recipes.observed_transport_attempts` from the
matching source checkout with its SDK installed. Install the openai, httpx,
and aiohttp dependencies from the contributor environment.

A loopback peer returns HTTP 503, then HTTP 200, without live credentials.
The borrowed client retries once. The caller counts transport entries before
each send, separately from peer receipts and completed model responses.
Entry does not prove a socket write, service receipt, success, or billing.
The counts cover only this transport and exclude other clients, native internal
requests, child clients, remote retries, and work before observation."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
from aiohttp import web
from openai import AsyncOpenAI

from agents import Agent, ModelRequest, ModelRequestOptions, NativeProvider, ResponsesModel


class ObservedTransport(httpx.AsyncBaseTransport):
    """The wrapper has been observing entry without retaining request payloads."""

    def __init__(self) -> None:
        self.entries = 0
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.entries += 1
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def run_example() -> dict[str, int | str]:
    peer_requests = 0
    adapter_invocations = 0

    def request_options(_request: ModelRequest) -> ModelRequestOptions:
        nonlocal adapter_invocations
        adapter_invocations += 1
        return {}

    async def respond(request: web.Request) -> web.Response:
        nonlocal peer_requests
        await request.read()
        peer_requests += 1
        if peer_requests == 1:
            return web.json_response(
                {"error": {"message": "Synthetic retry", "type": "server_error"}},
                status=503,
                headers={"retry-after": "0"},
            )
        return web.json_response(
            {
                "id": "resp_observed_attempts",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_observed_attempts",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    transport = ObservedTransport()
    try:
        # Application-owned sockets have been selecting the loopback port.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            await web.SockSite(runner, listener.dup()).start()
            url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1"
            async with (
                httpx.AsyncClient(transport=transport) as http_client,
                AsyncOpenAI(
                    api_key="synthetic",
                    base_url=url,
                    http_client=http_client,
                    max_retries=1,
                ) as client,
                NativeProvider(
                    model_backend=ResponsesModel(client, request_options=request_options),
                    supports_websockets=False,
                ) as provider,
            ):
                result = await Agent(
                    model="fixture-observed-attempts",
                    instructions="Answer briefly.",
                    provider=provider,
                ).run("Complete the synthetic request.")
                assert result.final_output == "Done."
                assert result.run_usage.requests == 1
                assert adapter_invocations == 1
                assert transport.entries == peer_requests == 2
                return {
                    "observed_transport_entries": transport.entries,
                    "loopback_peer_receipts": peer_requests,
                    "completed_model_responses": result.run_usage.requests,
                    "adapter_invocations": adapter_invocations,
                    "other_paths": "unavailable",
                }
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(asyncio.wait_for(run_example(), timeout=30))))
