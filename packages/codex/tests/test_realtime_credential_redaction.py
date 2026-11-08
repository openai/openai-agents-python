"""Synthetic-secret diagnostic regressions for native Realtime activation."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session


def test_config_repr_omits_hosted_mcp_credentials() -> None:
    config = RealtimeConfig(
        settings={
            "hosted_mcp_tools": [
                {
                    "server_label": "fixture",
                    "server_url": "https://example.invalid",
                    "authorization": "synthetic-hosted-authorization",
                    "headers": {"X-Private": "synthetic-private-header"},
                }
            ]
        }
    )
    assert "synthetic-hosted-authorization" not in repr(config)
    assert "synthetic-private-header" not in repr(config)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("phase", ["prepare", "activate"])
@pytest.mark.parametrize("custom", [False, True])
async def test_admission_failure_drops_credentials_from_sdk_traceback(
    cancel: bool, phase: str, custom: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-realtime-only-credential"
    authorization = "synthetic-hosted-authorization"
    header = "synthetic-private-header"
    failure = (
        asyncio.CancelledError("admission cancelled")
        if cancel
        else RuntimeError("provider rejected")
    )
    seen_key = False
    seen_settings = False

    async def native_call(operation: str, request: str) -> str:
        nonlocal seen_key, seen_settings
        params = json.loads(request)
        if operation == "realtime_conversation_prepare_sdk":
            hosted = params["settings"]["hosted_mcp_tools"][0]
            seen_settings = (
                hosted["authorization"] == authorization
                and hosted["headers"]["X-Private"] == header
            )
            if phase == "prepare":
                raise failure
            return json.dumps({"generation": params["generation"]})
        assert operation == "realtime_conversation_activate_sdk"
        seen_key = params["api_key"] == secret
        raise failure

    async def prepare(request: str, handle: Any) -> str:
        return await native_call("realtime_conversation_prepare_sdk", request)

    async def prepare_mcp() -> None:
        pass

    async def tools(selection: Any) -> tuple[list[Any], None]:
        return [], None

    async def connect(context: Any) -> None:
        raise AssertionError("The stub driver must not perform I/O")

    class Driver:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr("_agents_codex.realtime._RealtimeAdapterDriver", Driver)
    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(
                call=native_call, _prepare_realtime=prepare, _realtime_adapter=lambda _: object()
            ),
            _prepare_realtime_mcp=prepare_mcp,
            _realtime_function_tools=tools,
        ),
    )
    config = RealtimeConfig(
        api_key=lambda: secret,
        adapter=SimpleNamespace(connect=connect) if custom else None,
        settings={
            "hosted_mcp_tools": [
                {
                    "server_label": "fixture",
                    "server_url": "https://example.invalid",
                    "authorization": authorization,
                    "headers": {"X-Private": header},
                }
            ]
        },
    )
    voice = RealtimeSession(session, config=config)
    voice._live = True
    with pytest.raises(type(failure)) as caught:
        await voice._admit()
    assert seen_settings
    assert seen_key is (phase == "activate")
    assert caught.value is failure
    assert voice._credentials is None
    assert config.settings is not None
    assert config.settings["hosted_mcp_tools"][0]["authorization"] == authorization
    assert config.settings["hosted_mcp_tools"][0]["headers"]["X-Private"] == header
    frames = []
    tb = caught.value.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == RealtimeSession._admit.__code__.co_filename:
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    assert "_admit" in {frame.f_code.co_name for frame in frames}
    for frame in frames:
        assert all(value not in repr(frame.f_locals) for value in (secret, authorization, header))


@pytest.mark.parametrize("failure_phase", ["prepare", "activate", None])
async def test_startup_releases_owned_static_credentials_and_keeps_caller_config(
    failure_phase: str | None,
) -> None:
    secret = "synthetic-static-realtime-credential"
    authorization = "synthetic-static-hosted-authorization"
    header = "synthetic-static-private-header"
    failure = RuntimeError("original startup failure")
    prepared = False
    activated = False

    async def native_call(operation: str, request: str) -> str:
        nonlocal prepared, activated
        params = json.loads(request)
        if operation == "realtime_conversation_prepare_sdk":
            hosted = params["settings"]["hosted_mcp_tools"][0]
            assert hosted["authorization"] == authorization
            assert hosted["headers"]["X-Private"] == header
            prepared = True
            if failure_phase == "prepare":
                raise failure
            return json.dumps({"generation": params["generation"]})
        assert operation == "realtime_conversation_activate_sdk"
        assert params["api_key"] == secret
        activated = True
        if failure_phase == "activate":
            raise failure
        return json.dumps({"call_id": None, "sdp": None})

    async def prepare_mcp() -> None:
        pass

    async def tools(selection: Any) -> tuple[list[Any], None]:
        return [], None

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(call=native_call),
            _prepare_realtime_mcp=prepare_mcp,
            _realtime_function_tools=tools,
        ),
    )
    config = RealtimeConfig(
        model="fixture-realtime",
        guardrail_text_threshold=7,
        api_key=secret,
        settings={
            "hosted_mcp_tools": [
                {
                    "server_label": "fixture",
                    "server_url": "https://example.invalid",
                    "authorization": authorization,
                    "headers": {"X-Private": header},
                }
            ]
        },
    )
    voice = RealtimeSession(session, config=config)
    voice._live = True
    if failure_phase is None:
        await voice._admit()
    else:
        with pytest.raises(RuntimeError) as caught:
            await voice._admit()
        assert caught.value is failure
        tb = failure.__traceback__
        retained = []
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == RealtimeSession._admit.__code__.co_filename:
                retained.append(tb.tb_frame.f_locals["self"])
            tb = tb.tb_next
        assert retained
        assert all(item is voice for item in retained)
        # Follow the SDK-owned configuration references, including repr-hidden fields.
        # Caller Session/provider/application objects retain their own ownership.
        for item in retained:
            state = repr(vars(item._config)) + repr(item._settings)
            assert all(value not in state for value in (secret, authorization, header))
    assert prepared
    assert activated is (failure_phase != "prepare")
    assert voice._config.api_key is None
    assert voice._config.settings is None
    assert voice._settings == {}
    assert voice._config.model == "fixture-realtime"
    assert voice._config.guardrail_text_threshold == 7
    assert config.api_key == secret
    assert config.settings is not None
    assert config.settings["hosted_mcp_tools"][0]["authorization"] == authorization
    assert config.settings["hosted_mcp_tools"][0]["headers"]["X-Private"] == header


def test_webrtc_offer_credentials_are_hidden_from_direct_and_nested_repr() -> None:
    from agents.realtime import RealtimeWebRTC

    offer = "v=0\r\na=ice-ufrag:synthetic-ice-user\r\na=ice-pwd:synthetic-ice-password\r\n"
    transport = RealtimeWebRTC(offer)
    assert transport.offer_sdp == offer
    assert "synthetic-ice-user" not in repr(transport)
    assert "synthetic-ice-password" not in repr(transport)
    config = RealtimeConfig(transport=transport)
    assert config.transport is transport
    assert "synthetic-ice-user" not in repr(config)
    assert "synthetic-ice-password" not in repr(config)


@pytest.mark.parametrize(
    ("phase", "cancel"),
    [("prepare", False), ("activate", False), ("prepare", True), ("activate", True)],
)
async def test_failed_webrtc_startup_releases_sdk_owned_offer(phase: str, cancel: bool) -> None:
    from agents.realtime import RealtimeWebRTC

    offer = "v=0\r\na=ice-ufrag:synthetic-startup-user\r\na=ice-pwd:synthetic-startup-password\r\n"
    transport = RealtimeWebRTC(offer)
    config = RealtimeConfig(transport=transport)
    failure = (
        asyncio.CancelledError("startup cancelled") if cancel else RuntimeError("startup failed")
    )
    received = False

    async def native_call(operation: str, request: str) -> str:
        nonlocal received
        params = json.loads(request)
        if operation == "realtime_conversation_prepare_sdk":
            assert params["params"]["transport"] == {"type": "webrtc", "sdp": offer}
            received = True
            if phase == "prepare":
                raise failure
            return json.dumps({"generation": params["generation"]})
        assert operation == "realtime_conversation_activate_sdk"
        raise failure

    async def prepare_mcp() -> None:
        pass

    async def tools(selection: Any) -> tuple[list[Any], None]:
        return [], None

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(call=native_call),
            _prepare_realtime_mcp=prepare_mcp,
            _realtime_function_tools=tools,
        ),
    )
    voice = RealtimeSession(session, config=config)
    voice._live = True
    with pytest.raises(type(failure)) as caught:
        await voice._admit()
    assert caught.value is failure
    assert received
    frames = []
    tb = failure.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == RealtimeSession._admit.__code__.co_filename:
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    assert "_admit" in {frame.f_code.co_name for frame in frames}
    for frame in frames:
        assert "synthetic-startup-user" not in repr(frame.f_locals)
        assert "synthetic-startup-password" not in repr(frame.f_locals)
    assert voice._config.transport is None
    # Clearing SDK-owned aliases must not mutate caller-owned input.
    assert config.transport is transport
    assert transport.offer_sdp == offer


async def test_successful_webrtc_startup_preserves_public_answer() -> None:
    from agents.realtime import RealtimeWebRTC

    offer = "v=0\r\na=ice-pwd:synthetic-success-offer\r\n"
    answer = "v=0\r\na=ice-pwd:synthetic-success-answer\r\n"

    async def native_call(operation: str, request: str) -> str:
        params = json.loads(request)
        if operation == "realtime_conversation_prepare_sdk":
            assert params["params"]["transport"]["sdp"] == offer
            return json.dumps({"generation": params["generation"]})
        assert operation == "realtime_conversation_activate_sdk"
        return json.dumps({"call_id": "synthetic-webrtc-call", "sdp": answer})

    async def prepare_mcp() -> None:
        pass

    async def tools(selection: Any) -> tuple[list[Any], None]:
        return [], None

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(call=native_call),
            _prepare_realtime_mcp=prepare_mcp,
            _realtime_function_tools=tools,
        ),
    )
    transport = RealtimeWebRTC(offer)
    voice = RealtimeSession(session, config=RealtimeConfig(transport=transport))
    voice._live = True
    voice._start = asyncio.create_task(voice._admit())
    await voice._start
    assert await voice.answer_sdp() == answer
    assert voice.call_id == "synthetic-webrtc-call"
    assert voice._config.transport is transport
    assert transport.offer_sdp == offer
