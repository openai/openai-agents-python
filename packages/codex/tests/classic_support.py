"""Stable application callbacks shared by the original and relocated classic runtimes."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

# Only the historical fixture writer selects the unrelocated runtime.
_runtime_name = os.environ.get("CLASSIC_TEST_SOURCE_RUNTIME", "agents.classic")
runtime = importlib.import_module(_runtime_name)
if TYPE_CHECKING:
    from agents.classic import RunContextWrapper, function_tool
    from agents.classic.testing import ScriptedModel
else:
    ScriptedModel = importlib.import_module(f"{_runtime_name}.testing").ScriptedModel
    RunContextWrapper = runtime.RunContextWrapper
    function_tool = runtime.function_tool


class DeterministicModel(ScriptedModel):
    """Use the existing scripted Model subclass with the real Python runner."""


def text_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="message-classic",
        role="assistant",
        type="message",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def tool_call(name: str, label: str, call_id: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id=f"item-{call_id}",
        type="function_call",
        name=name,
        arguments=json.dumps({"label": label}),
        call_id=call_id,
    )


def instructions(context: Any, agent: Any) -> str:
    return "Use the supplied tools and finish with a short answer."


@function_tool
def record_effect(context: RunContextWrapper[dict[str, Any]], label: str) -> str:
    """Record one observable application effect."""
    context.context["effects"].append(label)
    return f"recorded:{label}"


@function_tool(needs_approval=True)
def approved_effect(context: RunContextWrapper[dict[str, Any]], label: str) -> str:
    """Record an application effect after approval."""
    context.context["effects"].append(label)
    return f"approved:{label}"


def approval_graph(*, writer: bool = False, reverse: bool = False) -> tuple[Any, Any, Any]:
    """Build same-name workers whose settings must retain their saved identities."""
    low_model = DeterministicModel()
    high_model = DeterministicModel(
        [
            [
                tool_call("record_effect", "before", "effect-before"),
                tool_call("approved_effect", "approved", "effect-approved"),
            ]
        ]
        if writer
        else [[text_message("resumed")]]
    )
    workers = [
        runtime.Agent(
            name="worker",
            instructions=instructions,
            tools=[record_effect, approved_effect],
            model=low_model,
            model_settings=runtime.ModelSettings(temperature=0.1),
        ),
        runtime.Agent(
            name="worker",
            instructions=instructions,
            tools=[record_effect, approved_effect],
            model=high_model,
            model_settings=runtime.ModelSettings(temperature=0.9),
        ),
    ]
    low, high = workers
    root = runtime.Agent(
        name="root",
        instructions=instructions,
        model=DeterministicModel(
            [
                [
                    ResponseFunctionToolCall(
                        id="item-handoff",
                        type="function_call",
                        name="choose_high",
                        arguments="{}",
                        call_id="handoff-high",
                    )
                ]
            ]
            if writer
            else []
        ),
        handoffs=[
            runtime.handoff(worker, tool_name_override=name)
            for worker, name in (
                [(high, "choose_high"), (low, "choose_low")]
                if reverse
                else [(low, "choose_low"), (high, "choose_high")]
            )
        ],
    )
    return root, low, high


async def write_historical_approval_fixture(path: str) -> None:
    """Write a paused run with the original source, never the relocated implementation."""
    assert runtime.__name__ == "agents", "Select the original runtime in a fresh process."
    root, low, high = approval_graph(writer=True)
    result = await runtime.Runner.run(
        root,
        "Record before, then request approval.",
        context={"effects": []},
        run_config=runtime.RunConfig(tracing_disabled=True),
    )
    assert result.last_agent is high
    assert len(result.interruptions) == 1
    assert result.context_wrapper.context["effects"] == ["before"]
    Path(path).write_text(json.dumps(result.to_state().to_json(), indent=2) + "\n")
    identity_module = importlib.import_module("agents._run_state_agent_identity")
    Path(path).with_suffix(".identity.json").write_text(
        json.dumps(
            {
                "writer_commit": "814de8b1cfdaf6c70212dca9ca23563dd0b789ac",
                "runtime_module": runtime.__name__,
                "agent_signatures": {
                    label: identity_module._agent_identity_signature(agent)
                    for label, agent in (("root", root), ("low", low), ("high", high))
                },
            },
            indent=2,
        )
        + "\n"
    )
