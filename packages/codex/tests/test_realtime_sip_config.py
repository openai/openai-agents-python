"""Python SIP configuration forwarding; native provider projection is tested separately."""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession, RealtimeSIP
from agents import Session


@pytest.mark.parametrize("explicit_tools", [False, True])
async def test_sip_config_prepares_captured_tools_without_connecting(explicit_tools: bool) -> None:
    requests: list[dict[str, Any]] = []
    selections: list[Any] = []
    discovery: list[bool] = []
    selected = object()
    provider_config = {"type": "realtime", "model": "fixture-realtime", "tools": []}

    async def call(operation: str, request: str) -> str:
        assert operation == "realtime_conversation_config_sdk"
        requests.append(json.loads(request))
        return json.dumps(provider_config)

    async def tools(selection: Any) -> tuple[list[dict[str, Any]], str]:
        selections.append(selection)
        return [{"name": "captured_tool"}], "captured-selection"

    async def prepare_mcp() -> None:
        discovery.append(True)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Projection must not resolve voice credentials or connect voice")

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(call=call),
            _check_open=lambda: None,
            _check_callback_operation=lambda _: None,
            _realtime_function_tools=tools,
            _prepare_realtime_mcp=prepare_mcp,
            _realtime=None,
        ),
    )
    config = RealtimeConfig(
        model="fixture-realtime",
        voice="marin",
        instructions="Accept this synthetic call.",
        transport=RealtimeSIP("rtc_synthetic"),
        tools=cast(Any, (selected,)) if explicit_tools else None,
        api_key=unexpected,
        adapter=cast(Any, SimpleNamespace(connect=unexpected)),
        settings={"temperature": 0.7},
        automatically_trigger_response_for_mcp_tool_calls=True,
    )
    result = await Session.build_realtime_sip_config(session, config=config)
    assert result == provider_config
    assert selections == [(selected,) if explicit_tools else None]
    assert discovery == ([] if explicit_tools else [True])
    assert session._realtime is None
    request = requests[0]
    assert "generation" not in request
    assert "api_key" not in request
    assert request["function_tools"] == [{"name": "captured_tool"}]
    assert request["expected_selection_id"] == "captured-selection"
    assert "hosted_mcp_auto_response" not in request
    assert request["settings"] == {"temperature": 0.7}
    assert request["params"]["transport"] == {"type": "websocket"}
    assert request["params"]["model"] == "fixture-realtime"
    assert request["params"]["voice"] == "marin"
    assert request["params"]["prompt"] == "Accept this synthetic call."
    assert config.settings == {"temperature": 0.7}


async def test_sip_config_default_and_native_rejection() -> None:
    async def call(operation: str, request: str) -> str:
        payload = json.loads(request)
        assert payload["function_tools"] is None
        assert payload["params"]["model"] is None
        assert "prompt" not in payload["params"]
        raise ValueError("Captured source cannot project this configuration")

    async def tools(selection: Any) -> tuple[None, None]:
        assert selection is None
        return None, None

    async def prepare_mcp() -> None:
        pass

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(call=call),
            _check_open=lambda: None,
            _check_callback_operation=lambda _: None,
            _realtime_function_tools=tools,
            _prepare_realtime_mcp=prepare_mcp,
            _realtime=None,
        ),
    )
    with pytest.raises(ValueError, match="Captured source"):
        await Session.build_realtime_sip_config(session)


async def test_sip_config_rejects_attachment_policy_before_preparing_tools() -> None:
    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Invalid call-accept settings must fail before discovery or native calls"
        )

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _check_open=lambda: None,
            _check_callback_operation=lambda _: None,
            _native=SimpleNamespace(call=unexpected),
            _prepare_realtime_mcp=unexpected,
            _realtime_function_tools=unexpected,
        ),
    )
    with pytest.raises(ValueError, match="subsequent SIP attachment"):
        await Session.build_realtime_sip_config(
            session,
            config=RealtimeConfig(automatically_trigger_response_for_mcp_tool_calls=False),
        )


async def test_sip_attachment_forwards_repeated_call_accept_options() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    async def tools(selection: Any) -> tuple[list[Any], None]:
        assert selection == ()
        return [], None

    async def call(operation: str, request: str) -> str:
        params = json.loads(request)
        requests.append((operation, params))
        if operation == "realtime_conversation_prepare_sdk":
            return json.dumps({"generation": params["generation"]})
        assert operation == "realtime_conversation_activate_sdk"
        return json.dumps({"call_id": "rtc_repeated_options", "sdp": None})

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _realtime_function_tools=tools,
            _native=SimpleNamespace(call=call),
        ),
    )
    voice = RealtimeSession(
        session,
        config=RealtimeConfig(
            model="fixture-realtime",
            voice="marin",
            instructions="Repeated accepted instructions.",
            tools=(),
            transport=RealtimeSIP("rtc_repeated_options"),
        ),
    )
    session._realtime = voice
    voice._live = True
    await voice._admit()
    params = requests[0][1]["params"]
    assert params["transport"] == {"type": "existing_call", "call_id": "rtc_repeated_options"}
    assert params["model"] == "fixture-realtime"
    assert params["voice"] == "marin"
    assert params["prompt"] == "Repeated accepted instructions."
    assert voice.call_id == "rtc_repeated_options"
