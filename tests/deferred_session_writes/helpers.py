from __future__ import annotations

import json
from typing import Any, Literal

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    StopAtTools,
    function_tool,
    output_guardrail,
)
from agents.agent import Agent as AgentType
from agents.items import TResponseInputItem
from agents.memory.openai_conversations_session import OpenAIConversationsSession
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession


@function_tool(name_override="write_thing", needs_approval=True)
def write_thing(query: str) -> str:
    return f"wrote:{query}"


@function_tool(name_override="write_other", needs_approval=True)
def write_other(query: str) -> str:
    return f"other:{query}"


@function_tool(name_override="look_up", needs_approval=False)
def look_up(query: str) -> str:
    return f"schema for {query}"


@output_guardrail
async def always_fine(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)


@output_guardrail
async def always_trips(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)


@output_guardrail
async def always_crashes(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    raise RuntimeError("guardrail crashed")


_DEFERRING_BEHAVIOR = StopAtTools(stop_at_tool_names=["finish"])


def _make_deferring_agent(
    tool_use_behavior: StopAtTools | Literal["run_llm_again"] = _DEFERRING_BEHAVIOR,
) -> Agent:
    """A gated write on the second model turn, so the resumed boundary has a prefix."""
    return Agent(
        name="deferred repro",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(
                    output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[look_up, write_thing],
        output_guardrails=[always_fine],
        tool_use_behavior=tool_use_behavior,
    )


def _make_multi_approval_agent(
    tool_use_behavior: StopAtTools | Literal["run_llm_again"] = _DEFERRING_BEHAVIOR,
) -> Agent:
    """One deferred model response carrying two approval-required calls."""
    return Agent(
        name="deferred repro (multi)",
        instructions="Call both tools.",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("write_other", {"query": "x"}, call_id="call_PARKED_2"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing, write_other],
        output_guardrails=[always_fine],
        tool_use_behavior=tool_use_behavior,
    )


_PREAMBLE_TEXT = "About to write the thing."


def _make_terminal_tool_agent(
    *,
    with_guardrails: bool = True,
    tripping: bool = False,
    crashing: bool = False,
    with_preamble: bool = False,
) -> Agent:
    """The approved tool is terminal, so the resume ends in a final output."""
    guardrails = [always_fine]
    if tripping:
        guardrails = [always_trips]
    if crashing:
        guardrails = [always_crashes]
    parked_response = [function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
    if with_preamble:
        parked_response = [assistant_message(_PREAMBLE_TEXT), *parked_response]
    return Agent(
        name="deferred repro (terminal)",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(output=parked_response),
            ]
        ),
        tools=[look_up, write_thing],
        output_guardrails=guardrails if with_guardrails else [],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["write_thing"]),
    )


def _make_partial_filter_handoff_agent() -> Agent:
    """Two gated calls plus a handoff whose filter drops exactly one resolved output.

    An ``input_filter`` is an arbitrary caller callable, so dropping a subset of the
    resolved outputs is a legitimate shape; the held batch must not settle a call whose
    output the filter took away.
    """
    from agents import HandoffInputData, handoff

    def drops_one_output(data: HandoffInputData) -> HandoffInputData:
        def keep(items: tuple) -> tuple:
            kept = []
            for item in items:
                raw = getattr(item, "raw_item", None)
                call_id = (
                    raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
                )
                if call_id == "call_PARKED_2" and item.type == "tool_call_output_item":
                    continue
                kept.append(item)
            return tuple(kept)

        return HandoffInputData(
            input_history=data.input_history,
            pre_handoff_items=keep(data.pre_handoff_items),
            new_items=keep(data.new_items),
        )

    target = Agent(
        name="target",
        instructions="x",
        model=ScriptedModel([ModelStep(output=[assistant_message("done")])]),
    )
    return Agent(
        name="deferred repro (partial filter)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("write_other", {"query": "x"}, call_id="call_PARKED_2"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing, write_other],
        handoffs=[handoff(target, input_filter=drops_one_output)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


class _FailingResumeSession(SimpleListSession):
    """Control append acknowledgement at the public Session boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None
        self.error = RuntimeError("session append failed")

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        failure, self.failure = self.failure, None
        if failure == "before":
            raise self.error
        await super().add_items(items)
        if failure == "after":
            raise self.error


async def _run(
    agent: Agent, run_input: Any, session: Any, *, streamed: bool
) -> RunResult | RunResultStreaming:
    if streamed:
        result = Runner.run_streamed(agent, run_input, session=session)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, run_input, session=session)


async def _serialized_round_trip(result: RunResult | RunResultStreaming, agent: Agent) -> RunState:
    return await RunState.from_json(agent, json.loads(json.dumps(result.to_state().to_json())))


def _held_write(items: Any) -> Any:
    return {
        "session_id": "test",
        "items": items,
        "before": None,
        "persisted_count": 0,
        "held": True,
        "current_response": {"turn": 0, "start": 0},
    }


def _call_ids(items: list[TResponseInputItem]) -> list[Any]:
    return [item.get("call_id") for item in items if item.get("type") == "function_call"]


def _orphaned_outputs(items: list[TResponseInputItem]) -> list[Any]:
    calls = set(_call_ids(items))
    return [
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") not in calls
    ]


def _parked_pair(items: list[TResponseInputItem]) -> list[str]:
    return [
        str(item.get("type"))
        for item in items
        if isinstance(item, dict) and item.get("call_id") == "call_PARKED"
    ]


async def _parked_and_approved(
    agent: Agent, session: Any, *, streamed: bool, resume_agent: Agent | None = None
) -> RunState:
    first = await _run(agent, "do the thing", session, streamed=streamed)
    assert len(first.interruptions) == 1
    state = await _serialized_round_trip(first, resume_agent or agent)
    state.approve(state.get_interruptions()[0])
    return state


_EXPECTED_PAIR = ["function_call", "function_call_output"]


def _make_two_park_agent() -> Agent:
    """Two approval-required calls on consecutive turns, so a resume can park again."""
    return Agent(
        name="deferred repro (two parks)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("write_thing", {"query": "a"}, call_id="call_A")]),
                ModelStep(output=[function_call("write_other", {"query": "b"}, call_id="call_B")]),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing, write_other],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


class _RecordingConversationsSession(OpenAIConversationsSession):
    """Stand-in carrying the Conversations class identity the settle checks.

    The real backend talks to the Conversations API; the settle only asks whether the
    session is one of these to decide that the batch needs the Conversations
    sanitization, so this records what would be sent instead of sending it.
    """

    def __init__(self) -> None:
        self.session_id = "conv-1"
        self.added: list[TResponseInputItem] = []

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        return []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.added.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return None

    async def clear_session(self) -> None:
        return None
