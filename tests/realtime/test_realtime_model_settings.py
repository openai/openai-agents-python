from __future__ import annotations

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
from agents.tool import function_tool


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
    base_settings: RealtimeSessionModelSettings = {"model_name": "gpt-realtime"}
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
    assert merged["model_name"] == "gpt-realtime"
    assert merged["tracing"] is None
    assert base_settings == {"model_name": "gpt-realtime"}


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
