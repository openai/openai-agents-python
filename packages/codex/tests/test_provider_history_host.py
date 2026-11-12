from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.models import _capture_provider_history
from _agents_codex.session import _prepare_opening_model_settings
from agents import Agent, ConversationsHistory, ProviderHistory


def test_history_selection_detaches_current_identity():
    source: ProviderHistory = {"type": "conversation", "id": "conv_current"}
    captured = _capture_provider_history(source)
    source["id"] = "conv_changed"
    assert captured == {"type": "conversation", "id": "conv_current"}
    assert _capture_provider_history({"type": "previous_response"}) == {"type": "previous_response"}


@pytest.mark.parametrize(
    "value",
    [
        {"type": "conversation"},
        {"type": "conversation", "id": "  "},
        {"type": "previous_response", "id": ""},
        {"type": "other", "id": "resp_other"},
    ],
)
def test_history_selection_rejects_invalid_current_shape(value):
    with pytest.raises(ValueError):
        _capture_provider_history(cast(ProviderHistory, value))


@pytest.mark.parametrize("current", [False, None])
def test_saved_storage_cannot_authorize_current_http_chain(current):
    agent = Agent(
        instructions="Current declaration.",
        model="current-model",
        model_settings={} if current is None else {"store": current},
    )
    with pytest.raises(ValueError, match="current store=True"):
        _prepare_opening_model_settings(
            agent,
            "current-model",
            "Current declaration.",
            None,
            ("current-model", {"request_controls": {"store": True}}),
            None,
            [],
            {"type": "previous_response", "id": "resp_current"},
        )


@pytest.mark.parametrize("current", [False, None, True])
def test_conversation_restore_uses_current_storage_choice(current):
    agent = Agent(
        instructions="Current declaration.",
        model="current-model",
        model_settings={} if current is None else {"store": current},
    )
    _, settings = _prepare_opening_model_settings(
        agent,
        "current-model",
        "Current declaration.",
        None,
        ("current-model", {"request_controls": {"store": not current}}),
        None,
        [],
        {"type": "conversation", "id": "conv_current"},
    )
    assert settings["request_controls"].get("store") is current


def test_current_http_storage_grant_replaces_saved_omission():
    _, settings = _prepare_opening_model_settings(
        Agent(instructions="Current declaration.", model_settings={"store": True}),
        "current-model",
        "Current declaration.",
        None,
        ("current-model", {"request_controls": {"store": False}}),
        None,
        [],
        {"type": "previous_response", "id": "resp_current"},
    )
    assert settings["request_controls"]["store"] is True


def test_current_profile_cannot_inherit_root_storage_grant():
    agent = Agent(instructions="Current declaration.", model_settings={"store": True})
    with pytest.raises(ValueError, match="current store=True"):
        _prepare_opening_model_settings(
            agent,
            "current-model",
            "Current declaration.",
            None,
            ("current-model", {"request_controls": {"store": True}}),
            cast(Any, SimpleNamespace(profile=SimpleNamespace(id="selected"))),
            [{"id": "selected", "model": {"request_controls": {"has_policy": False}}}],
            {"type": "previous_response", "id": "resp_current"},
        )


def test_legacy_restore_keeps_its_existing_storage_settings():
    _, settings = _prepare_opening_model_settings(
        Agent(instructions="Current declaration.", model_settings={"store": False}),
        "current-model",
        "Current declaration.",
        None,
        ("current-model", {"request_controls": {"store": True}}),
        None,
        [],
    )
    assert settings["request_controls"]["store"] is True


class ReachedSessionBoundary(RuntimeError):
    pass


@pytest.mark.parametrize("entry", ["session", "run"])
async def test_public_entry_captures_history_before_opening_wait(monkeypatch, entry):
    entered = asyncio.Event()
    release = asyncio.Event()
    captured = []

    @asynccontextmanager
    async def boundary(self, **options):
        captured.append(options)
        entered.set()
        await release.wait()
        raise ReachedSessionBoundary("native boundary")
        yield

    monkeypatch.setattr(Agent, "_session_context", boundary)
    source: ProviderHistory = {"type": "conversation", "id": "conv_current"}
    agent = Agent(instructions="Current declaration.")

    async def open_entry():
        if entry == "run":
            await agent.run("Hello.", provider_history=source)
        else:
            async with agent.session(provider_history=source):
                raise AssertionError("the boundary must refuse before publication")

    task = asyncio.create_task(open_entry())
    await asyncio.wait_for(entered.wait(), timeout=5)
    source["id"] = "conv_changed"
    release.set()
    with pytest.raises(ReachedSessionBoundary):
        await task
    assert captured[0]["provider_history"] == {
        "type": "conversation",
        "id": "conv_current",
    }
    assert not captured[0].get("_private_text_run")


def test_run_sync_forwards_current_history(monkeypatch):
    captured = []

    @asynccontextmanager
    async def boundary(self, **options):
        captured.append(options)
        raise ReachedSessionBoundary("native boundary")
        yield

    monkeypatch.setattr(Agent, "_session_context", boundary)
    with pytest.raises(ReachedSessionBoundary):
        Agent(instructions="Current declaration.").run_sync(
            "Hello.", provider_history={"type": "conversation", "id": "conv_current"}
        )
    assert captured[0]["provider_history"] == {
        "type": "conversation",
        "id": "conv_current",
    }


async def test_conversations_bridge_refuses_competing_wire_history():
    history = ConversationsHistory(cast(Any, object()))
    with pytest.raises(ValueError, match="fresh native Session history owner"):
        await history.run(
            Agent(instructions="Current declaration."),
            "Hello.",
            provider_history={"type": "conversation", "id": "conv_other"},
        )


@pytest.mark.parametrize("profile_store", [True, False, None])
def test_idle_profile_storage_uses_selected_current_declaration(profile_store):
    agent = Agent(instructions="Current.", model_settings={"store": not profile_store})
    profile = SimpleNamespace(profile=SimpleNamespace(id="selected"))
    roster = [
        {"id": "selected", "model": {"request_controls": {"store": profile_store}}, "handoffs": {}}
    ]
    args = (agent, "current-model", "Current.", None, None, profile, roster)
    if profile_store is not True:
        with pytest.raises(ValueError, match="current store=True"):
            _prepare_opening_model_settings(*args, {"type": "previous_response"})
    else:
        _, settings = _prepare_opening_model_settings(*args, {"type": "previous_response"})
        assert settings["request_controls"]["store"] is True


def test_manual_history_restore_keeps_current_root_transport():
    _, settings = _prepare_opening_model_settings(
        Agent(
            instructions="Current declaration.",
            model_settings={
                "store": True,
                "extra_headers": {"x-history-route": "current"},
            },
        ),
        "current-model",
        "Current declaration.",
        None,
        (
            "current-model",
            {
                "request_controls": {
                    "store": True,
                    "request_transport": {"headers": {"x-history-route": "saved"}},
                }
            },
        ),
        None,
        [],
        {"type": "previous_response", "id": "resp_current"},
    )
    assert settings["request_controls"]["request_transport"]["headers"] == {
        "x-history-route": "current"
    }
