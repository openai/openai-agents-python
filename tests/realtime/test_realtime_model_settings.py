from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from openai.types.realtime.realtime_session_create_request import (
    RealtimeSessionCreateRequest,
)
from openai.types.realtime.session_update_event import SessionUpdateEvent

from agents.handoffs import Handoff
from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig, RealtimeSessionModelSettings
from agents.realtime.handoffs import realtime_handoff
from agents.realtime.model import RealtimeModelConfig
from agents.realtime.openai_realtime import (
    OpenAIRealtimeSIPModel,
    OpenAIRealtimeWebSocketModel,
    _build_model_settings_from_agent,
    _collect_enabled_handoffs,
)
from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool, function_tool


def _disabled_billing_realtime_handoff(*, is_enabled: Any = False) -> Handoff[Any, Any]:
    return realtime_handoff(
        RealtimeAgent(name="billing"),
        tool_name_override="transfer_to_billing",
        is_enabled=is_enabled,
    )


def _disabled_billing_realtime_tool(*, is_enabled: Any = False) -> FunctionTool:
    return function_tool(
        lambda: "ok",
        name_override="transfer_to_billing",
        is_enabled=is_enabled,
    )


@pytest.mark.asyncio
async def test_collect_enabled_handoffs_filters_disabled() -> None:
    parent = RealtimeAgent(name="parent")
    disabled = realtime_handoff(
        RealtimeAgent(name="child_disabled"),
        is_enabled=lambda ctx, agent: False,
    )
    parent.handoffs = [disabled, RealtimeAgent(name="child_enabled")]

    enabled = await _collect_enabled_handoffs(parent, RunContextWrapper(None))

    assert len(enabled) == 1
    assert isinstance(enabled[0], Handoff)
    assert enabled[0].agent_name == "child_enabled"


@pytest.mark.asyncio
async def test_build_model_settings_from_agent_merges_agent_fields(monkeypatch: pytest.MonkeyPatch):
    agent = RealtimeAgent(name="root", prompt={"id": "prompt-id"})
    monkeypatch.setattr(agent, "get_system_prompt", AsyncMock(return_value="sys"))

    @function_tool
    def helper() -> str:
        """Helper tool for testing."""
        return "ok"

    monkeypatch.setattr(agent, "get_all_tools", AsyncMock(return_value=[helper]))
    agent.handoffs = [RealtimeAgent(name="handoff-child")]
    base_settings: RealtimeSessionModelSettings = {"model_name": "gpt-realtime-2"}
    starting_settings: RealtimeSessionModelSettings = {"voice": "verse"}
    run_config: RealtimeRunConfig = {"tracing_disabled": True}

    merged = await _build_model_settings_from_agent(
        agent=agent,
        context_wrapper=RunContextWrapper(None),
        base_settings=base_settings,
        starting_settings=starting_settings,
        run_config=run_config,
    )

    assert merged["prompt"] == {"id": "prompt-id"}
    assert merged["instructions"] == "sys"
    assert merged["tools"][0].name == helper.name
    assert merged["handoffs"][0].agent_name == "handoff-child"
    assert merged["voice"] == "verse"
    assert merged["model_name"] == "gpt-realtime-2"
    assert merged["tracing"] is None
    assert base_settings == {"model_name": "gpt-realtime-2"}


@pytest.mark.asyncio
async def test_build_model_settings_filters_disabled_starting_handoff_name_conflict():
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    disabled_handoff = _disabled_billing_realtime_handoff()
    agent = RealtimeAgent(name="parent", tools=[tool])

    merged = await _build_model_settings_from_agent(
        agent=agent,
        context_wrapper=RunContextWrapper(None),
        base_settings={},
        starting_settings={"handoffs": [disabled_handoff]},
        run_config=None,
    )

    assert merged["tools"] == [tool]
    assert merged["handoffs"] == []


@pytest.mark.asyncio
async def test_build_model_settings_filters_disabled_starting_tool_name_conflict():
    disabled_tool = _disabled_billing_realtime_tool()
    handoff = _disabled_billing_realtime_handoff(is_enabled=True)
    agent = RealtimeAgent(name="parent", handoffs=[handoff])

    merged = await _build_model_settings_from_agent(
        agent=agent,
        context_wrapper=RunContextWrapper(None),
        base_settings={},
        starting_settings={"tools": [disabled_tool]},
        run_config=None,
    )

    assert merged["tools"] == []
    assert merged["handoffs"] == [handoff]


@pytest.mark.asyncio
async def test_build_model_settings_evaluates_starting_tool_is_enabled_callable():
    calls: list[tuple[RunContextWrapper[Any], RealtimeAgent[Any]]] = []

    async def is_enabled(ctx: RunContextWrapper[Any], agent_arg: RealtimeAgent[Any]) -> bool:
        calls.append((ctx, agent_arg))
        return False

    disabled_tool = _disabled_billing_realtime_tool(is_enabled=is_enabled)
    agent = RealtimeAgent(name="parent")
    context_wrapper = RunContextWrapper(None)

    merged = await _build_model_settings_from_agent(
        agent=agent,
        context_wrapper=context_wrapper,
        base_settings={},
        starting_settings={"tools": [disabled_tool]},
        run_config=None,
    )

    assert merged["tools"] == []
    assert calls == [(context_wrapper, agent)]


@pytest.mark.asyncio
async def test_build_model_settings_does_not_reevaluate_agent_handoff_without_override():
    call_count = 0

    async def is_enabled(ctx: RunContextWrapper[Any], agent_arg: RealtimeAgent[Any]) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count == 1

    handoff = cast(
        Handoff[Any, Any],
        realtime_handoff(RealtimeAgent(name="billing"), is_enabled=is_enabled),
    )
    agent = RealtimeAgent(name="parent", handoffs=[handoff])

    merged = await _build_model_settings_from_agent(
        agent=agent,
        context_wrapper=RunContextWrapper(None),
        base_settings={},
        starting_settings={"voice": "verse"},
        run_config=None,
    )

    assert merged["handoffs"] == [handoff]
    assert call_count == 1


@pytest.mark.asyncio
async def test_sip_model_build_initial_session_payload(monkeypatch: pytest.MonkeyPatch):
    agent = RealtimeAgent(name="parent", prompt={"id": "prompt-99"})
    child_agent = RealtimeAgent(name="child")
    agent.handoffs = [child_agent]

    @function_tool
    def ping() -> str:
        """Ping tool used for session payload building."""
        return "pong"

    monkeypatch.setattr(agent, "get_system_prompt", AsyncMock(return_value="parent-system"))
    monkeypatch.setattr(agent, "get_all_tools", AsyncMock(return_value=[ping]))

    model_config: RealtimeModelConfig = {
        "initial_model_settings": {
            "model_name": "gpt-realtime-mini",
            "voice": "verse",
        }
    }
    run_config: RealtimeRunConfig = {
        "model_settings": {"output_modalities": ["text"]},
        "tracing_disabled": True,
    }
    overrides: RealtimeSessionModelSettings = {
        "audio": {"input": {"format": {"type": "audio/pcmu"}}},
        "output_audio_format": "g711_ulaw",
    }

    payload = await OpenAIRealtimeSIPModel.build_initial_session_payload(
        agent,
        context={"user": "abc"},
        model_config=model_config,
        run_config=run_config,
        overrides=overrides,
    )

    assert isinstance(payload, RealtimeSessionCreateRequest)
    assert payload.model == "gpt-realtime-mini"
    assert payload.output_modalities == ["text"]
    assert payload.audio is not None
    audio = payload.audio
    assert audio.input is not None
    assert audio.input.format is not None
    assert audio.input.format.type == "audio/pcmu"
    assert audio.output is not None
    assert audio.output.format is not None
    assert audio.output.format.type == "audio/pcmu"
    assert audio.output.voice == "verse"
    assert payload.instructions == "parent-system"
    assert payload.prompt is not None and payload.prompt.id == "prompt-99"
    tool_names: set[str] = set()
    for tool in payload.tools or []:
        name = getattr(tool, "name", None)
        if name:
            tool_names.add(name)
    assert ping.name in tool_names
    assert f"transfer_to_{child_agent.name}" in tool_names


@pytest.mark.asyncio
async def test_sip_initial_session_payload_filters_disabled_initial_model_settings_handoff():
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    disabled_handoff = _disabled_billing_realtime_handoff()
    agent = RealtimeAgent(name="parent", tools=[tool])

    payload = await OpenAIRealtimeSIPModel.build_initial_session_payload(
        agent,
        model_config={"initial_model_settings": {"handoffs": [disabled_handoff]}},
    )

    tool_names = [getattr(tool, "name", None) for tool in payload.tools or []]
    assert tool_names.count("transfer_to_billing") == 1


@pytest.mark.asyncio
async def test_sip_initial_session_payload_filters_disabled_initial_model_settings_tool():
    disabled_tool = _disabled_billing_realtime_tool()
    agent = RealtimeAgent(
        name="parent",
        handoffs=[_disabled_billing_realtime_handoff(is_enabled=True)],
    )

    payload = await OpenAIRealtimeSIPModel.build_initial_session_payload(
        agent,
        model_config={"initial_model_settings": {"tools": [disabled_tool]}},
    )

    tool_names = [getattr(tool, "name", None) for tool in payload.tools or []]
    assert tool_names == ["transfer_to_billing"]


@pytest.mark.asyncio
async def test_sip_initial_session_payload_filters_disabled_override_handoff():
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    disabled_handoff = _disabled_billing_realtime_handoff()
    agent = RealtimeAgent(name="parent", tools=[tool])

    payload = await OpenAIRealtimeSIPModel.build_initial_session_payload(
        agent,
        overrides={"handoffs": [disabled_handoff]},
    )

    tool_names = [getattr(tool, "name", None) for tool in payload.tools or []]
    assert tool_names.count("transfer_to_billing") == 1


@pytest.mark.asyncio
async def test_sip_initial_session_payload_filters_disabled_override_tool():
    disabled_tool = _disabled_billing_realtime_tool()
    agent = RealtimeAgent(
        name="parent",
        handoffs=[_disabled_billing_realtime_handoff(is_enabled=True)],
    )

    payload = await OpenAIRealtimeSIPModel.build_initial_session_payload(
        agent,
        overrides={"tools": [disabled_tool]},
    )

    tool_names = [getattr(tool, "name", None) for tool in payload.tools or []]
    assert tool_names == ["transfer_to_billing"]


@pytest.mark.asyncio
async def test_sip_initial_session_payload_does_not_reevaluate_agent_handoff_without_override():
    call_count = 0

    async def is_enabled(ctx: RunContextWrapper[Any], agent_arg: RealtimeAgent[Any]) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count == 1

    handoff = cast(
        Handoff[Any, Any],
        realtime_handoff(RealtimeAgent(name="billing"), is_enabled=is_enabled),
    )
    agent = RealtimeAgent(name="parent", handoffs=[handoff])

    payload = await OpenAIRealtimeSIPModel.build_initial_session_payload(
        agent,
        overrides={"voice": "verse"},
    )

    tool_names = [getattr(tool, "name", None) for tool in payload.tools or []]
    assert "transfer_to_billing" in tool_names
    assert call_count == 1


def test_call_id_session_update_omits_null_audio_formats() -> None:
    model = OpenAIRealtimeWebSocketModel()
    model._call_id = "call_123"

    session_config = model._get_session_config({})
    payload = SessionUpdateEvent(type="session.update", session=session_config).model_dump(
        exclude_unset=True
    )

    audio = payload["session"]["audio"]
    assert "format" not in audio["input"]
    assert "format" not in audio["output"]


def test_call_id_session_update_includes_explicit_audio_formats() -> None:
    model = OpenAIRealtimeWebSocketModel()
    model._call_id = "call_123"

    session_config = model._get_session_config(
        {
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
        }
    )
    payload = SessionUpdateEvent(type="session.update", session=session_config).model_dump(
        exclude_unset=True
    )

    audio = payload["session"]["audio"]
    assert audio["input"]["format"]["type"] == "audio/pcmu"
    assert audio["output"]["format"]["type"] == "audio/pcmu"
