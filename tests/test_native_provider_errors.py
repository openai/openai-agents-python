"""Provider error metadata through real native HTTP and Session lifetimes."""

from __future__ import annotations

import asyncio
import copy
import pickle
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    InteractionRequired,
    NativeError,
    NativeProvider,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.test_native_provider_websockets import peer as websocket_peer


@dataclass
class ProviderErrorPeer(ResponsesFixture):
    # The shared fixture supplies SSE; provider failures must occur before SSE starts.
    failure: tuple[int, object, str] | None = None
    failed_requests: int = 0
    failed_inputs: list[dict[str, Any]] = field(default_factory=list)
    failure_started: asyncio.Event | None = None
    failure_release: asyncio.Event | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        failure = self.failure
        if failure is None:
            return await super().handle(request)
        self.failed_requests += 1
        self.failed_inputs.append(await request.json())
        status, code, kind = failure
        if self.failure_started is not None:
            self.failure_started.set()
        if self.failure_release is not None:
            await self.failure_release.wait()
        return web.json_response(
            {
                "error": {
                    "message": "Synthetic provider rejection",
                    "type": kind,
                    "code": code,
                    "param": None,
                }
            },
            status=status,
            headers={"Retry-After": "0", "x-request-id": "synthetic-provider-request"},
        )


@pytest_asyncio.fixture(loop_scope="function")
async def provider_peer() -> AsyncIterator[ProviderErrorPeer]:
    peer = ProviderErrorPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/v1"))
        yield peer


@pytest.mark.parametrize("exception_type", [NativeError, InteractionRequired])
@pytest.mark.parametrize(
    "args",
    [
        (),
        ("local failure",),
        ("local failure", "second detail", 17),
        (
            "unexpected status 401 Unauthorized: insufficient_quota, "
            "request id: fabricated-provider-id",
        ),
    ],
)
def test_native_errors_preserve_existing_exception_behavior(
    exception_type: type[NativeError], args: tuple[Any, ...]
) -> None:
    error = exception_type(*args)
    with pytest.raises(RuntimeError) as caught:
        raise error
    assert caught.value is error
    assert error.args == args
    assert str(error) == str(RuntimeError(*args))
    assert error.status_code is None
    assert error.code is None
    assert error.request_id is None
    assert error.category is None


@pytest.mark.parametrize(
    "restore",
    [copy.copy, copy.deepcopy, lambda error: pickle.loads(pickle.dumps(error))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_native_error_metadata_survives_standard_exception_copying(
    restore: Callable[[NativeError], NativeError],
) -> None:
    error = NativeError(
        "existing description",
        "existing second argument",
        status_code=401,
        code="invalid_api_key",
        request_id="synthetic-provider-request",
        category="authentication_error",
    )
    copied = restore(error)
    assert isinstance(copied, NativeError)
    assert copied is not error
    assert copied.args == error.args
    assert str(copied) == str(RuntimeError(*error.args))
    assert copied.status_code == 401
    assert copied.code == "invalid_api_key"
    assert copied.request_id == "synthetic-provider-request"
    assert copied.category == "authentication_error"


@pytest.mark.parametrize(
    ("failure", "status_code", "code", "request_id", "category"),
    [
        pytest.param(
            (401, "invalid_api_key", "invalid_request_error"),
            401,
            "invalid_api_key",
            "synthetic-provider-request",
            "authentication_error",
            id="authentication",
        ),
        pytest.param(
            (429, "insufficient_quota", "insufficient_quota"),
            None,
            None,
            None,
            "usage_limit_exceeded",
            id="quota",
        ),
        pytest.param(
            (429, "rate_limit_exceeded", "rate_limit_error"),
            429,
            None,
            "synthetic-provider-request",
            "rate_limit_exceeded",
            id="rate-limit",
        ),
        pytest.param(
            (401, {"message": "synthetic extra diagnostics"}, "invalid_request_error"),
            401,
            None,
            "synthetic-provider-request",
            "authentication_error",
            id="authentication-code-is-object",
        ),
        pytest.param(
            (401, "invalid_api_key\nsynthetic extra diagnostics", "invalid_request_error"),
            401,
            None,
            "synthetic-provider-request",
            "authentication_error",
            id="authentication-code-is-not-an-identifier",
        ),
        pytest.param(
            (401, "x" * 129, "invalid_request_error"),
            401,
            None,
            "synthetic-provider-request",
            "authentication_error",
            id="authentication-code-too-long",
        ),
    ],
)
async def test_http_provider_failures_expose_only_metadata_retained_by_core(
    provider_peer: ProviderErrorPeer,
    failure: tuple[int, object, str],
    status_code: int | None,
    code: str | None,
    request_id: str | None,
    category: str,
) -> None:
    provider_peer.failure = failure
    provider = NativeProvider(
        base_url=provider_peer.url,
        api_key="synthetic-loopback-only",
        supports_websockets=False,
    )
    agent = Agent(instructions="Reply briefly.", provider=provider)
    try:
        async with timeout(30):
            with pytest.raises(NativeError) as caught:
                await agent.run("Synthetic local error probe.")
        error = caught.value
        assert provider_peer.failed_requests >= 1
        assert error.status_code == status_code
        assert error.code == code
        assert error.request_id == request_id
        assert error.category == category
        assert isinstance(error, RuntimeError)
        assert len(error.args) == 1 and isinstance(error.args[0], str)
        assert str(error) == error.args[0]
        assert not hasattr(error, "body")
        assert not hasattr(error, "response")
        assert not hasattr(error, "headers")
    finally:
        await provider.aclose()


async def test_websocket_unauthorized_keeps_available_provider_metadata() -> None:
    async with timeout(30), websocket_peer(upgrade_status=401) as peer:
        agent = Agent(instructions="Answer.", provider=peer.provider(key="synthetic-rejected-key"))
        async with agent.session() as session:
            with pytest.raises(NativeError, match="401") as caught:
                await (await session.submit("must fail")).get_result()
        error = caught.value
        assert peer.upgrades
        assert isinstance(error, RuntimeError)
        assert len(error.args) == 1 and isinstance(error.args[0], str)
        assert str(error) == error.args[0]
        assert error.status_code == 401
        assert error.code == "invalid_api_key"
        assert error.request_id is None
        assert error.category == "authentication_error"
        assert not hasattr(error, "body")
        assert not hasattr(error, "response")
        assert not hasattr(error, "headers")


async def test_provider_metadata_does_not_reach_later_turns_or_local_errors(
    provider_peer: ProviderErrorPeer,
) -> None:
    local_text = "unexpected status 429 Too Many Requests, request id: local-fabricated"

    @function_tool
    async def local_failure() -> str:
        raise NativeError(local_text)

    provider = NativeProvider(
        base_url=provider_peer.url,
        api_key="synthetic-loopback-only",
        supports_websockets=False,
    )
    agent = Agent(
        instructions="Reply briefly or use the supplied tool.",
        provider=provider,
        tools=[local_failure],
    )
    try:
        async with timeout(30), agent.session() as session:
            provider_peer.failure = (401, "invalid_api_key", "invalid_request_error")
            with pytest.raises(NativeError) as first:
                await (await session.submit("Fail at the provider.")).get_result()
            assert first.value.status_code == 401
            assert first.value.code == "invalid_api_key"
            assert first.value.request_id == "synthetic-provider-request"
            assert first.value.category == "authentication_error"

            provider_peer.failure = None
            provider_peer.respond(assistant_output("successful later turn"))
            completed = await (
                await session.submit("Reply after the provider recovers.")
            ).get_result()
            assert completed.final_output == "successful later turn"

            provider_peer.respond(function_call("local_failure", {}, "local-failure-call"))
            with pytest.raises(NativeError, match="local-fabricated") as last:
                await (await session.submit("Now call the local tool.")).get_result()
            assert str(last.value) == local_text
            assert last.value.status_code is None
            assert last.value.code is None
            assert last.value.request_id is None
            assert last.value.category is None
    finally:
        await provider.aclose()


async def test_terminal_http_auth_failure_stops_after_one_refresh(
    provider_peer: ProviderErrorPeer,
) -> None:
    provider_peer.failure = (401, "invalid_api_key", "invalid_request_error")
    async with NativeProvider(
        base_url=provider_peer.url, api_key="synthetic-loopback-only", supports_websockets=False
    ) as provider:
        with pytest.raises(NativeError) as caught:
            await Agent(instructions="Answer.", model="gpt-5.2", provider=provider).run(
                "Reject the captured key."
            )
    assert provider_peer.failed_requests == 2
    assert caught.value.status_code == 401
    assert caught.value.code == "invalid_api_key"
    assert caught.value.request_id == "synthetic-provider-request"
    assert caught.value.category == "authentication_error"


async def test_terminal_http_auth_failure_preserves_one_prior_tool_effect(
    provider_peer: ProviderErrorPeer,
) -> None:
    effects = []

    @function_tool
    def write_once() -> str:
        effects.append("committed")
        provider_peer.failure = (401, "invalid_api_key", "invalid_request_error")
        return "receipt-1"

    provider_peer.respond(function_call("write_once", {}, "write-once-call"))
    async with NativeProvider(
        base_url=provider_peer.url, api_key="synthetic-loopback-only", supports_websockets=False
    ) as provider:
        with pytest.raises(NativeError) as caught:
            await Agent(
                instructions="Use write_once then answer.",
                model="gpt-5.2",
                provider=provider,
                tools=[write_once],
            ).run("Record one effect.")
    assert effects == ["committed"]
    assert len(provider_peer.requests) == 1
    assert provider_peer.failed_requests == 2
    assert caught.value.status_code == 401
    assert caught.value.code == "invalid_api_key"
    assert caught.value.request_id == "synthetic-provider-request"
    assert caught.value.category == "authentication_error"
    for request in provider_peer.failed_inputs:
        original = next(item for item in request["input"] if item.get("type") == "function_call")
        outputs = [item for item in request["input"] if item.get("type") == "function_call_output"]
        assert len(outputs) == 1 and outputs[0]["call_id"] == original["call_id"]
        assert outputs[0]["output"] == "receipt-1"


@pytest.mark.parametrize("status,expected_http", [(401, 2), (403, 6)])
async def test_websocket_auth_fallback_keeps_final_http_failure(
    status: int, expected_http: int
) -> None:
    async with (
        timeout(30),
        websocket_peer(
            upgrade_status=401, http_failure=(status, "final_http_rejection", "final-http-request")
        ) as peer,
    ):
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.2",
            provider=peer.provider(key="synthetic-rejected-key"),
        )
        async with agent.session() as session:
            with pytest.raises(NativeError) as caught:
                await (await session.submit("Reject the upgrade then reject HTTP.")).get_result()
        assert caught.value.status_code == status
        assert caught.value.code == "final_http_rejection"
        assert caught.value.request_id == "final-http-request"
        assert caught.value.category == "authentication_error"
        assert len(peer.upgrades) == 2
        assert len(peer.http_requests) == expected_http
        assert all(
            header.get("authorization") == "Bearer synthetic-rejected-key"
            for header in [*peer.upgrades, *peer.request_headers]
        )


async def test_cancel_after_tool_effect_prevents_late_auth_retry(
    provider_peer: ProviderErrorPeer,
) -> None:
    effects = []
    provider_peer.failure_started = asyncio.Event()
    provider_peer.failure_release = asyncio.Event()

    @function_tool
    def write_once() -> str:
        effects.append("committed")
        provider_peer.failure = (401, "invalid_api_key", "invalid_request_error")
        return "receipt-1"

    provider_peer.respond(function_call("write_once", {}, "write-once-call"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=provider_peer.url, api_key="synthetic-loopback-only", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            instructions="Use write_once then answer.",
            model="gpt-5.2",
            provider=provider,
            tools=[write_once],
        )
        async with agent.session() as session:
            turn = await session.submit("Record one effect then hold the auth rejection.")
            try:
                await provider_peer.failure_started.wait()
                assert effects == ["committed"]
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            finally:
                provider_peer.failure_release.set()
            provider_peer.failure = None
            provider_peer.respond(assistant_output("fresh turn"))
            assert (
                await (await session.submit("Answer after cancellation.")).get_result()
            ).final_output == "fresh turn"
    assert effects == ["committed"]
    assert provider_peer.failed_requests == 1
    assert len(provider_peer.requests) == 2
