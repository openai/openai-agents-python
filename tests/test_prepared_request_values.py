"""Pure preparation-value boundaries do not require a candidate native binary."""

from __future__ import annotations

import json
from typing import Any

import pytest

import agents
from agents.prepared_request import _capturing_callback


def test_public_prepared_types_are_lazy_exports() -> None:
    from agents.prepared_request import PreparedRequest, PreparedToolPresentation

    assert agents.PreparedRequest is PreparedRequest
    assert agents.PreparedToolPresentation is PreparedToolPresentation
    assert {"PreparedRequest", "PreparedToolPresentation"} <= set(agents.__all__)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_callback_result_is_frozen_before_retained_value_changes(asynchronous: bool) -> None:
    value = {"instructions": "original", "input": [], "tools": []}

    def sync() -> dict[str, Any]:
        return value

    async def async_callback() -> dict[str, Any]:
        return value

    invoke = _capturing_callback(async_callback if asynchronous else sync, lambda: True)
    captured = await invoke() if asynchronous else invoke()
    value["instructions"] = "late mutation"
    assert json.loads(captured) == {"instructions": "original", "input": [], "tools": []}


@pytest.mark.parametrize("value", [{1: "key"}, {"input": ()}, {"number": float("nan")}])
def test_callback_return_rejects_silently_coerced_json(value: Any) -> None:
    with pytest.raises(TypeError, match="prepare_request"):
        _capturing_callback(lambda: value, lambda: True)()


def test_retired_callback_does_not_serialize_late_application_result() -> None:
    class LateResult(dict):
        def items(self):
            pytest.fail("late result must not be serialized")

    assert _capturing_callback(lambda: LateResult(secret="late"), lambda: False)() is None


async def test_default_websocket_permission_rejects_before_opening() -> None:
    from agents import Agent

    with pytest.raises(ValueError, match="supports_websockets=False"):
        await Agent(instructions="No connection").run(
            "Try",
            prepare_request=lambda turn, request, **indices: None,
        )


@pytest.mark.parametrize("profile", ["deferred", "code", "hosted"])
async def test_unsupported_profile_rejects_before_opening(profile: str) -> None:
    from agents import Agent, NativeProvider, WebSearch, function_tool

    @function_tool(defer_loading=profile == "deferred")
    def ordinary() -> str:
        return "unused"

    with pytest.raises(ValueError, match="prepare_request"):
        await Agent(
            instructions="No connection",
            provider=NativeProvider(supports_websockets=False),
            tools=[WebSearch()] if profile == "hosted" else [ordinary],
            programmatic_tool_calling=profile == "code",
        ).run("Try", prepare_request=lambda turn, request, **indices: None)


async def test_agent_repair_profile_rejects_before_session_factory(monkeypatch) -> None:
    from pydantic import BaseModel

    from agents import Agent, NativeProvider, PromptedOutput

    class Answer(BaseModel):
        value: int

    def must_not_open(*args, **kwargs):
        pytest.fail("repair-enabled editor opened Session resources")

    monkeypatch.setattr(Agent, "session", must_not_open)
    with pytest.raises(ValueError, match="correction attempts"):
        await Agent(
            instructions="No I/O",
            provider=NativeProvider(supports_websockets=False),
            output_type=PromptedOutput(Answer, max_retries=1),
        ).run("Reject repair", prepare_request=lambda turn, request, **indices: None)
