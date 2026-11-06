"""Provider error metadata through real native HTTP and Session lifetimes."""

from __future__ import annotations

import asyncio
import copy
import json
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


@pytest.mark.parametrize("status,expected_requests", [(401, 2), (403, 1)])
async def test_terminal_http_auth_failure_stops_after_bounded_recovery(
    provider_peer: ProviderErrorPeer, status: int, expected_requests: int
) -> None:
    provider_peer.failure = (status, "invalid_api_key", "invalid_request_error")
    async with NativeProvider(
        base_url=provider_peer.url, api_key="synthetic-loopback-only", supports_websockets=False
    ) as provider:
        with pytest.raises(NativeError) as caught:
            await Agent(instructions="Answer.", model="gpt-5.2", provider=provider).run(
                "Reject the captured key."
            )
    assert provider_peer.failed_requests == expected_requests
    assert caught.value.status_code == status
    assert caught.value.code == "invalid_api_key"
    assert caught.value.request_id == "synthetic-provider-request"
    assert caught.value.category == "authentication_error"


@pytest.mark.parametrize("status,expected_requests", [(401, 2), (403, 1)])
async def test_terminal_http_auth_failure_preserves_one_prior_tool_effect(
    provider_peer: ProviderErrorPeer, status: int, expected_requests: int
) -> None:
    effects = []

    @function_tool
    def write_once() -> str:
        effects.append("committed")
        provider_peer.failure = (status, "invalid_api_key", "invalid_request_error")
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
    assert provider_peer.failed_requests == expected_requests
    assert caught.value.status_code == status
    assert caught.value.code == "invalid_api_key"
    assert caught.value.request_id == "synthetic-provider-request"
    assert caught.value.category == "authentication_error"
    for request in provider_peer.failed_inputs:
        original = next(item for item in request["input"] if item.get("type") == "function_call")
        outputs = [item for item in request["input"] if item.get("type") == "function_call_output"]
        assert len(outputs) == 1 and outputs[0]["call_id"] == original["call_id"]
        assert outputs[0]["output"] == "receipt-1"


@pytest.mark.parametrize(
    "upgrade_status,status,expected_upgrades,expected_http",
    [(401, 401, 2, 2), (401, 403, 2, 1), (426, 401, 1, 2), (426, 403, 1, 1)],
)
async def test_websocket_startup_auth_fallback_keeps_final_http_failure(
    upgrade_status: int, status: int, expected_upgrades: int, expected_http: int
) -> None:
    async with (
        timeout(30),
        websocket_peer(
            upgrade_status=upgrade_status,
            http_failure=(status, "final_http_rejection", "final-http-request"),
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
        assert len(peer.upgrades) == expected_upgrades
        assert all(
            json.loads(headers["x-codex-turn-metadata"])["request_kind"] == "prewarm"
            for headers in peer.upgrades
        )
        assert len(peer.http_requests) == expected_http
        assert all(request.get("generate") is not False for request in peer.http_requests)
        assert all(
            header.get("authorization") == "Bearer synthetic-rejected-key"
            for header in [*peer.upgrades, *peer.request_headers]
        )


@pytest.mark.parametrize("status", [401, 403])
async def test_sampling_websocket_auth_recovery_is_spent_before_http_fallback(status: int) -> None:
    steps = []

    def before_step(turn: Any, step_index: int) -> list[Any]:
        steps.append(step_index)
        return []

    async with timeout(30), websocket_peer() as peer:
        peer.respond(assistant_output("socket ready"))
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.2",
            provider=peer.provider(key="synthetic-rejected-key"),
        )
        async with agent.session() as session:
            assert (
                await (await session.submit("Prime the existing socket.")).get_result()
            ).final_output == "socket ready"
            assert peer.warmups == 1 and not peer.http_requests
            prior_upgrades = len(peer.upgrades)
            # Retire the warmed socket so the next actual turn must reconnect.
            await asyncio.gather(*(socket.close() for socket in tuple(peer.sockets)))
            await peer.disconnected.wait()
            peer.upgrade_status = 401
            peer.http_failure = (status, "final_http_rejection", "final-http-request")
            with pytest.raises(NativeError) as caught:
                await (
                    await session.submit("Reject the sampling reconnect.", before_step=before_step)
                ).get_result()
        assert steps == [0]
        rejected_upgrades = peer.upgrades[prior_upgrades:]
        assert len(rejected_upgrades) == 2
        for headers in rejected_upgrades:
            metadata = json.loads(headers["x-codex-turn-metadata"])
            assert metadata["request_kind"] == "turn" and metadata["turn_id"]
            assert headers.get("authorization") == "Bearer synthetic-rejected-key"
        assert len(peer.http_requests) == 1
        assert peer.http_requests[0].get("generate") is not False
        assert caught.value.status_code == status
        assert caught.value.code == "final_http_rejection"
        assert caught.value.request_id == "final-http-request"
        assert caught.value.category == "authentication_error"


async def test_http_auth_recovery_budget_resets_for_each_model_request(
    provider_peer: ProviderErrorPeer,
) -> None:
    provider_peer.failure = (401, "invalid_api_key", "invalid_request_error")
    async with (
        timeout(30),
        NativeProvider(
            base_url=provider_peer.url, api_key="synthetic-loopback-only", supports_websockets=False
        ) as provider,
    ):
        async with Agent(
            instructions="Answer.", model="gpt-5.2", provider=provider
        ).session() as session:
            for request_index in range(2):
                with pytest.raises(NativeError) as caught:
                    await (
                        await session.submit("Reject this independent model request.")
                    ).get_result()
                assert provider_peer.failed_requests == 2 * (request_index + 1)
                assert caught.value.status_code == 401
                assert caught.value.code == "invalid_api_key"
                assert caught.value.request_id == "synthetic-provider-request"
                assert caught.value.category == "authentication_error"


@pytest.mark.parametrize("status", [401, 403])
async def test_cancel_after_tool_effect_prevents_late_auth_retry(
    provider_peer: ProviderErrorPeer, status: int
) -> None:
    effects = []
    provider_peer.failure_started = asyncio.Event()
    provider_peer.failure_release = asyncio.Event()

    @function_tool
    def write_once() -> str:
        effects.append("committed")
        provider_peer.failure = (status, "invalid_api_key", "invalid_request_error")
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


@pytest.mark.parametrize(
    "reason",
    [
        "undecodable_output_item",
        "provider_owned_tool_receipt",
        "unfinished_assistant_item",
        "missing_supported_terminal_output",
    ],
)
def test_captured_integrity_reason_is_readonly_and_does_not_change_error_metadata(
    reason: str,
) -> None:
    from _agents_codex.session import _model_failure_error, _native_error

    error = _model_failure_error(
        {"message": "fixed safe message", "response_integrity_reason": reason}
    )
    assert error.response_integrity_reason == reason
    assert str(error) == "fixed safe message"
    assert (error.status_code, error.code, error.request_id, error.category) == (
        None,
        None,
        None,
        None,
    )
    assert error.effects == "unknown"
    with pytest.raises(AttributeError):
        error.response_integrity_reason = None  # type: ignore[misc]
    assert copy.copy(error).response_integrity_reason == reason
    assert pickle.loads(pickle.dumps(error)).response_integrity_reason == reason
    # A terminal or provider body is not a captured ModelFailure notice.
    assert (
        _native_error(
            {"message": "same category", "response_integrity_reason": reason}
        ).response_integrity_reason
        is None
    )
    assert NativeError("application error").response_integrity_reason is None


def test_absent_captured_integrity_reason_remains_unknown() -> None:
    from _agents_codex.session import _model_failure_error

    assert _model_failure_error({"message": "safe"}).response_integrity_reason is None
    assert (
        _model_failure_error(
            {"message": "safe", "response_integrity_reason": None}
        ).response_integrity_reason
        is None
    )


def test_passive_reason_conversion_preserves_its_originating_category_and_provider_fields() -> None:
    from _agents_codex.session import _native_error

    error = _native_error(
        {
            "message": "safe",
            "codex_error_info": "Other",
            "_sdk_response_integrity_reason": "undecodable_output_item",
            "_sdk_provider_error": {
                "status_code": 401,
                "code": "synthetic_code",
                "request_id": "synthetic-id",
            },
        }
    )
    assert error.response_integrity_reason == "undecodable_output_item"
    assert error.category == "internal_error"
    assert (error.status_code, error.code, error.request_id) == (
        401,
        "synthetic_code",
        "synthetic-id",
    )
