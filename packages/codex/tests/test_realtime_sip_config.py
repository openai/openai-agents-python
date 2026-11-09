"""Python SIP configuration forwarding; native provider projection is tested separately."""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSIP
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
        automatically_trigger_response_for_mcp_tool_calls=False,
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
    assert request["hosted_mcp_auto_response"] is False
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
        ),
    )
    with pytest.raises(ValueError, match="Captured source"):
        await Session.build_realtime_sip_config(session)
