from __future__ import annotations

import dataclasses
import json

import pytest
from pydantic import BaseModel

from agents import Agent, Runner, function_tool
from agents.run_state import RunState

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


class _User(BaseModel):
    name: str
    tier: str = "free"


@dataclasses.dataclass
class _Team:
    label: str


@function_tool(needs_approval=True)
def _guarded(x: str) -> str:
    """Guarded.

    Args:
        x: value
    """
    return "ok"


def _interrupting_agent() -> Agent:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("_guarded", json.dumps({"x": "1"}), call_id="c1")],
            [get_text_message("done")],
        ]
    )
    return Agent(name="A", model=model, tools=[_guarded])


@pytest.mark.asyncio
async def test_mapping_context_holding_models_serializes_as_structured_data():
    """A mapping context holding models must serialize rather than crash.

    The mapping branch shallow-copied with `dict()`, leaving Pydantic models and dataclasses
    in place, so `to_string()` failed inside `json.dumps` with a bare `TypeError` that never
    mentioned the context.
    """
    agent = _interrupting_agent()
    context = {
        "user": _User(name="ada"),
        "roles": [_User(name="b", tier="pro")],
        "teams": {"core": [_Team(label="t")]},
    }

    result = await Runner.run(agent, "hi", context=context)
    payload = result.to_state().to_string()

    stored = json.loads(payload)["context"]["context"]
    # Structured data rather than reprs, with defaulted fields preserved.
    assert stored["user"] == {"name": "ada", "tier": "free"}
    assert stored["roles"] == [{"name": "b", "tier": "pro"}]
    assert stored["teams"] == {"core": [{"label": "t"}]}


@pytest.mark.asyncio
async def test_mapping_context_state_round_trips_and_resumes():
    agent = _interrupting_agent()
    result = await Runner.run(agent, "hi", context={"user": _User(name="ada")})

    payload = result.to_state().to_string()
    restored = await RunState.from_string(agent, payload)
    assert (
        json.loads(restored.to_string())["context"]["context"]
        == (json.loads(payload)["context"]["context"])
    )

    restored.approve(result.interruptions[0])
    resumed = await Runner.run(agent, restored)
    assert resumed.final_output == "done"


@pytest.mark.asyncio
async def test_plain_json_mapping_context_is_unchanged():
    """Contexts that were already JSON-compatible must serialize exactly as before."""
    agent = _interrupting_agent()
    context = {"a": 1, "b": ["x", {"c": True}], "d": None}

    result = await Runner.run(agent, "hi", context=context)
    stored = json.loads(result.to_state().to_string())["context"]["context"]

    assert stored == context
