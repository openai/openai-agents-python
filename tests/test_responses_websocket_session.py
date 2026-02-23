import importlib

import pytest

from agents import Agent, responses_websocket_session
from agents.models.multi_provider import MultiProvider
from agents.models.openai_provider import OpenAIProvider


@pytest.mark.asyncio
async def test_responses_websocket_session_builds_shared_run_config():
    async with responses_websocket_session() as ws:
        assert isinstance(ws.provider, OpenAIProvider)
        assert ws.provider._use_responses is True
        assert ws.provider._use_responses_websocket is True
        assert isinstance(ws.run_config.model_provider, MultiProvider)
        assert ws.run_config.model_provider.openai_provider is ws.provider


@pytest.mark.asyncio
async def test_responses_websocket_session_preserves_openai_prefix_routing(monkeypatch):
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_get_model(model_name):
        captured["model_name"] = model_name
        return sentinel

    async with responses_websocket_session() as ws:
        monkeypatch.setattr(ws.provider, "get_model", fake_get_model)

        result = ws.run_config.model_provider.get_model("openai/gpt-4.1")

        assert result is sentinel
        assert captured["model_name"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_responses_websocket_session_run_streamed_injects_run_config(monkeypatch):
    agent = Agent(name="test", instructions="Be concise.", model="gpt-4")
    captured = {}
    sentinel = object()

    def fake_run_streamed(starting_agent, input, **kwargs):
        captured["starting_agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        return sentinel

    ws_module = importlib.import_module("agents.responses_websocket_session")
    monkeypatch.setattr(ws_module.Runner, "run_streamed", fake_run_streamed)

    async with responses_websocket_session() as ws:
        result = ws.run_streamed(agent, "hello")

        assert result is sentinel
        assert captured["starting_agent"] is agent
        assert captured["input"] == "hello"
        assert captured["kwargs"]["run_config"] is ws.run_config


@pytest.mark.asyncio
async def test_responses_websocket_session_run_injects_run_config(monkeypatch):
    agent = Agent(name="test", instructions="Be concise.", model="gpt-4")
    captured = {}
    sentinel = object()

    async def fake_run(starting_agent, input, **kwargs):
        captured["starting_agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        return sentinel

    ws_module = importlib.import_module("agents.responses_websocket_session")
    monkeypatch.setattr(ws_module.Runner, "run", fake_run)

    async with responses_websocket_session() as ws:
        result = await ws.run(agent, "hello")

        assert result is sentinel
        assert captured["starting_agent"] is agent
        assert captured["input"] == "hello"
        assert captured["kwargs"]["run_config"] is ws.run_config


@pytest.mark.asyncio
async def test_responses_websocket_session_rejects_run_config_override():
    agent = Agent(name="test", instructions="Be concise.", model="gpt-4")

    async with responses_websocket_session() as ws:
        with pytest.raises(ValueError, match="run_config"):
            ws.run_streamed(agent, "hello", run_config=object())


@pytest.mark.asyncio
async def test_responses_websocket_session_context_manager_closes_provider(monkeypatch):
    close_calls: list[OpenAIProvider] = []

    async def fake_aclose(self):
        close_calls.append(self)

    monkeypatch.setattr(OpenAIProvider, "aclose", fake_aclose)

    async with responses_websocket_session() as ws:
        provider = ws.provider

    assert close_calls == [provider]


@pytest.mark.asyncio
async def test_responses_websocket_session_does_not_expose_run_sync():
    async with responses_websocket_session() as ws:
        assert not hasattr(ws, "run_sync")
