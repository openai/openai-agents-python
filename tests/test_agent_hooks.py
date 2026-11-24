from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from typing import Any

import pytest
from typing_extensions import TypedDict

from agents.agent import Agent, AgentBase
from agents.guardrail import GuardrailFunctionOutput, output_guardrail
from agents.handoffs import HandoffInputData, handoff
from agents.lifecycle import AgentHooks
from agents.result import RunResult, RunResultStreaming
from agents.run import Runner
from agents.run_config import CallModelData, ModelInputData, RunConfig
from agents.run_context import AgentHookContext, RunContextWrapper, TContext
from agents.run_inbox import MessageReceipt, RunInbox
from agents.run_state import RunState
from agents.testing import ModelCall, ModelStep, ScriptedModel
from agents.tool import Tool, function_tool
from agents.tool_context import ToolContext

from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)


class AgentHooksForTests(AgentHooks):
    def __init__(self):
        self.events: dict[str, int] = defaultdict(int)
        self.tool_context_ids: list[str] = []

    def reset(self):
        self.events.clear()
        self.tool_context_ids.clear()

    async def on_start(self, context: AgentHookContext[TContext], agent: Agent[TContext]) -> None:
        self.events["on_start"] += 1

    async def on_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        output: Any,
    ) -> None:
        self.events["on_end"] += 1

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        source: Agent[TContext],
    ) -> None:
        self.events["on_handoff"] += 1

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
    ) -> None:
        self.events["on_tool_start"] += 1
        if isinstance(context, ToolContext):
            self.tool_context_ids.append(context.tool_call_id)

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
        result: object,
    ) -> None:
        self.events["on_tool_end"] += 1
        if isinstance(context, ToolContext):
            self.tool_context_ids.append(context.tool_call_id)


class FalsyAgentHooks(AgentHooksForTests):
    def __bool__(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_falsy_agent_hooks_are_invoked() -> None:
    hooks = FalsyAgentHooks()
    model = ScriptedModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
    )
    model.extend(
        [
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            [get_text_message("done")],
        ]
    )

    await Runner.run(agent, input="user_message")

    assert hooks.events == {
        "on_start": 1,
        "on_tool_start": 1,
        "on_tool_end": 1,
        "on_end": 1,
    }


@pytest.mark.asyncio
async def test_non_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = ScriptedModel()
    agent_1 = Agent(
        name="test_1",
        model=model,
    )
    agent_2 = Agent(
        name="test_2",
        model=model,
    )
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
    )

    agent_1.handoffs.append(agent_3)

    model.enqueue([get_text_message("user_message")])
    output = await Runner.run(agent_3, input="user_message")
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

    model.extend(
        [
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_1")],
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")
    assert len(hooks.tool_context_ids) == 2
    assert len(set(hooks.tool_context_ids)) == 1
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: text message
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs once
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_1")],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_2"),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: text message
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # Only one tool call
        "on_tool_end": 2,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


@pytest.mark.asyncio
async def test_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = ScriptedModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
    )

    agent_1.handoffs.append(agent_3)

    model.enqueue([get_text_message("user_message")])
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: text message
            [get_text_message("done")],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs twice
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_1")],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_2"),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: text message
            [get_text_message("done")],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # Only one tool call
        "on_tool_end": 2,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


class Foo(TypedDict):
    a: str


@pytest.mark.asyncio
async def test_structured_output_non_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = ScriptedModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
        output_type=Foo,
    )

    agent_1.handoffs.append(agent_3)

    model.enqueue([get_final_output_message(json.dumps({"a": "b"}))])
    output = await Runner.run(agent_3, input="user_message")
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: end message (for agent 1)
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs twice
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_1")],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_2"),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: end message (for agent 3)
            [get_final_output_message(json.dumps({"a": "b"}))],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # Only one tool call
        "on_tool_end": 2,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


@pytest.mark.asyncio
async def test_structured_output_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = ScriptedModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
        output_type=Foo,
    )

    agent_1.handoffs.append(agent_3)

    model.enqueue([get_final_output_message(json.dumps({"a": "b"}))])
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: end message (for agent 1)
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")
    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs twice
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_1")],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_2"),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: end message (for agent 3)
            [get_final_output_message(json.dumps({"a": "b"}))],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # 2 tool calls
        "on_tool_end": 2,  # 2 tool calls
        "on_handoff": 1,  # 1 handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


class EmptyAgentHooks(AgentHooks):
    pass


@pytest.mark.asyncio
async def test_base_agent_hooks_dont_crash():
    hooks = EmptyAgentHooks()
    model = ScriptedModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
        output_type=Foo,
    )
    agent_1.handoffs.append(agent_3)

    model.enqueue([get_final_output_message(json.dumps({"a": "b"}))])
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: end message (for agent 1)
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    model.extend(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: end message (for agent 3)
            [get_final_output_message(json.dumps({"a": "b"}))],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass


class AgentHooksWithTurnInput(AgentHooks):
    """Agent hooks that capture turn_input at the start and end of each task."""

    def __init__(self):
        self.captured_turn_inputs: list[list[Any]] = []
        self.end_inputs: list[list[Any]] = []

    async def on_start(self, context: AgentHookContext[TContext], agent: Agent[TContext]) -> None:
        self.captured_turn_inputs.append(list(context.turn_input))

    async def on_end(
        self, context: AgentHookContext[TContext], agent: Agent[TContext], output: Any
    ) -> None:
        self.end_inputs.append(list(context.turn_input))


async def _run_with_inbox(
    agent: Agent[Any],
    inbox: RunInbox,
    streamed: bool,
    *,
    value: str | RunState[Any] = "first task",
    server_managed: bool = False,
    run_config: RunConfig | None = None,
    context: RunContextWrapper[Any] | None = None,
) -> RunResult | RunResultStreaming:
    if not streamed:
        return await Runner.run(
            agent,
            value,
            context=context,
            inbox=inbox,
            auto_previous_response_id=server_managed,
            run_config=run_config,
        )
    stream = Runner.run_streamed(
        agent,
        value,
        context=context,
        inbox=inbox,
        auto_previous_response_id=server_managed,
        run_config=run_config,
    )
    async for _ in stream.stream_events():
        pass
    return stream


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("server_managed", [False, True])
async def test_follow_up_turn_input_survives_tool_continuation(
    streamed: bool, queued: bool, server_managed: bool
) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()
    first = [{"role": "user", "content": "first task"}]
    second = [{"role": "user", "content": "second task"}]

    def first_answer(call: ModelCall) -> list[Any]:
        if queued:
            inbox.queue("second task")
        else:
            inbox.steer("second task")
        return [get_text_message("first answer")]

    def instructions(context: RunContextWrapper[Any], agent: Agent[Any]) -> str:
        return str(context.turn_input[0].get("content"))

    def enabled(context: RunContextWrapper[Any], agent: AgentBase[Any]) -> bool:
        return context.turn_input == second

    @function_tool(is_enabled=enabled)
    async def work() -> str:
        inbox.steer("clarification")
        return "done"

    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [get_function_tool_call("work", "{}")],
            [get_text_message("second answer")],
        ]
    )
    agent = Agent(name="test", model=model, hooks=hooks, tools=[work], instructions=instructions)
    result = await _run_with_inbox(agent, inbox, streamed, server_managed=server_managed)

    assert hooks.captured_turn_inputs == hooks.end_inputs == [first, second]
    assert [call.system_instructions for call in model.calls] == [
        "first task",
        "second task",
        "second task",
    ]
    assert [bool(call.tools) for call in model.calls] == [False, True, True]
    all_messages = ["first task", "second task", "clarification"]
    for history in (result.to_input_list(), result.to_input_list(mode="normalized")):
        assert [
            item.get("content") for item in history if item.get("role") == "user"
        ] == all_messages
    transmitted = [
        item.get("content") for item in model.calls[-1].input if item.get("role") == "user"
    ]
    assert transmitted == (["clarification"] if server_managed else all_messages)
    assert (model.calls[-1].previous_response_id is not None) == server_managed
    if server_managed:
        assert [item.get("type") or item.get("role") for item in model.calls[-1].input] == [
            "function_call_output",
            "user",
        ]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("also_admitted", [None, "other instruction", "second task"])
async def test_filtered_follow_up_keeps_task_input_on_server_retry(
    streamed: bool, also_admitted: str | None
) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()
    task = ["second task"] + ([also_admitted] if also_admitted else [])
    expected = [{"role": "user", "content": text} for text in task]

    def first_answer(call: ModelCall) -> list[Any]:
        if also_admitted:
            for message in task:
                inbox.steer(message)
        else:
            inbox.queue(task[0])
        return [get_text_message("first answer")]

    def instructions(context: RunContextWrapper[Any], agent: Agent[Any]) -> str:
        return " | ".join(str(item.get("content")) for item in context.turn_input)

    def enabled(context: RunContextWrapper[Any], agent: AgentBase[Any]) -> bool:
        return context.turn_input == expected

    @function_tool(is_enabled=enabled)
    async def work() -> str:
        # A new steer with identical text belongs to the ongoing tool turn.
        inbox.steer(task[0])
        return "done"

    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [get_function_tool_call("work", "{}")],
            [get_text_message("second answer")],
        ]
    )

    def omit_first_occurrence(data: CallModelData[Any]) -> ModelInputData:
        if len(model.calls) == 1:
            data.model_data.input.pop(0)
        return data.model_data

    agent = Agent(name="test", model=model, hooks=hooks, tools=[work], instructions=instructions)
    await _run_with_inbox(
        agent,
        inbox,
        streamed,
        server_managed=True,
        run_config=RunConfig(call_model_input_filter=omit_first_occurrence),
    )
    assert [item.get("content") for item in model.calls[1].input] == task[1:]
    assert [item.get("content") for item in model.calls[2].input if item.get("role") == "user"] == [
        task[0],
        task[0],
    ]
    assert model.calls[2].system_instructions == " | ".join(task)
    assert model.calls[2].tools
    assert hooks.end_inputs[-1] == expected


@pytest.mark.parametrize("streamed", [False, True])
async def test_cleared_pending_input_cannot_rejoin_follow_up_by_position(streamed: bool) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.steer("kept")
        inbox.steer("withheld")
        return [get_text_message("first answer")]

    @function_tool(needs_approval=True)
    async def work() -> str:
        return "done"

    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [get_function_tool_call("work", "{}")],
            [get_text_message("second answer")],
        ]
    )

    def omit_last_occurrence(data: CallModelData[Any]) -> ModelInputData:
        if len(model.calls) == 1:
            data.model_data.input.pop()
        return data.model_data

    agent = Agent(name="test", model=model, hooks=hooks, tools=[work])
    paused = await _run_with_inbox(
        agent,
        inbox,
        streamed,
        server_managed=True,
        run_config=RunConfig(call_model_input_filter=omit_last_occurrence),
    )
    state = paused.to_state()
    assert [item.get("content") for item in state.pending_input] == ["withheld"]
    state.clear_pending_input()
    state.add_input("replacement")
    state.approve(state.get_interruptions()[0])
    await _run_with_inbox(agent, RunInbox(), streamed, value=state)
    assert [
        item.get("content") for item in model.calls[-1].input if item.get("role") == "user"
    ] == ["replacement"]
    assert hooks.end_inputs[-1] == [{"role": "user", "content": "kept"}]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "server_managed,omission",
    [(False, None), (True, None), (True, "all"), (True, "partial")],
    ids=["local", "server", "server-withheld", "server-partly-withheld"],
)
async def test_follow_up_turn_input_survives_approval_resume(
    streamed: bool, server_managed: bool, omission: str | None
) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()
    task = ["second task"]
    if omission == "partial":
        task.append("second instruction")
    second = [{"role": "user", "content": text} for text in task]
    effects: list[str] = []
    receipts: list[MessageReceipt] = []

    def first_answer(call: ModelCall) -> list[Any]:
        if omission == "partial":
            receipts.extend(inbox.steer(text) for text in task)
        else:
            receipts.append(inbox.queue(task[0]))
        return [get_text_message("first answer")]

    def omit_first(data: CallModelData[Any]) -> ModelInputData:
        if len(model.calls) == 1:
            data.model_data.input.pop(0)
        return data.model_data

    def enabled(context: RunContextWrapper[Any], agent: AgentBase[Any]) -> bool:
        return context.turn_input == second

    @function_tool(is_enabled=enabled, needs_approval=True)
    async def work() -> str:
        effects.append("approved")
        return "done"

    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [get_function_tool_call("work", "{}")],
            [get_text_message("second answer")],
        ]
    )
    agent = Agent(name="test", model=model, hooks=hooks, tools=[work])
    config = RunConfig(call_model_input_filter=omit_first) if omission else None
    paused = await _run_with_inbox(
        agent, inbox, streamed, server_managed=server_managed, run_config=config
    )
    assert len(paused.interruptions) == 1 and not effects
    state = paused.to_state()
    outcomes = [await receipt.wait() for receipt in receipts]
    if omission:
        assert [item.get("content") for item in state.pending_input] == task[:1]
        assert [item.get("content") for item in model.calls[1].input] == task[1:]
        assert outcomes[0].status == "not_submitted" and outcomes[0].retained
        assert all(outcome.status == "submitted" for outcome in outcomes[1:])
    state.approve(state.get_interruptions()[0])
    resumed = await _run_with_inbox(agent, RunInbox(), streamed, value=state)
    assert resumed.final_output == "second answer"
    assert effects == ["approved"]
    assert hooks.end_inputs[-1] == second
    assert model.calls[-1].tools
    assert (model.calls[-1].previous_response_id is not None) == server_managed
    if omission:
        assert [
            item.get("content") for item in model.calls[-1].input if item.get("role") == "user"
        ] == task[:1]
        assert not state.pending_input
    assert [await receipt.wait() for receipt in receipts] == outcomes


@pytest.mark.parametrize("streamed", [False, True])
async def test_follow_up_keeps_output_guardrail_history_across_approval_pauses(
    streamed: bool,
) -> None:
    inbox = RunInbox()

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue("second task")
        return [get_text_message("first answer")]

    @output_guardrail
    def check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=output, tripwire_triggered=False)

    @function_tool(needs_approval=True)
    async def work() -> str:
        return "done"

    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [get_function_tool_call("work", "{}", call_id=key) for key in ("one", "two")],
            [get_text_message("second answer")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[work], output_guardrails=[check])
    result = await _run_with_inbox(agent, inbox, streamed)
    for remaining in (2, 1):
        assert len(result.interruptions) == remaining
        assert [r.output.output_info for r in result.output_guardrail_results] == ["first answer"]
        state = result.to_state()
        if remaining == 1:
            state = await RunState.from_json(agent, state.to_json())
        state.approve(state.get_interruptions()[0])
        result = await _run_with_inbox(agent, RunInbox(), streamed, value=state)

    assert result.final_output == "second answer"
    assert [r.output.output_info for r in result.output_guardrail_results] == [
        "first answer",
        "second answer",
    ]


@pytest.mark.parametrize("streamed", [False, True])
async def test_delayed_approval_checkpoint_isolated_from_reused_context(streamed: bool) -> None:
    context = RunContextWrapper(context=None)
    hooks = AgentHooksWithTurnInput()
    effects: list[str] = []

    @function_tool(needs_approval=True)
    async def work() -> str:
        effects.append("approved")
        return "done"

    first_model = ScriptedModel(
        [
            [get_function_tool_call("work", "{}")],
            [get_text_message("finished")],
        ]
    )
    first = Agent(name="first", model=first_model, tools=[work], hooks=hooks)
    paused = await _run_with_inbox(first, RunInbox(), streamed, context=context)
    assert len(paused.interruptions) == 1 and not effects

    inbox = RunInbox()

    def other_answer(call: ModelCall) -> list[Any]:
        inbox.steer("other instruction")
        inbox.steer("another instruction")
        return [get_text_message("other answer")]

    other_model = ScriptedModel([ModelStep.respond(other_answer), [get_text_message("done")]])

    def withhold_both(data: CallModelData[Any]) -> ModelInputData:
        if len(other_model.calls) == 1:
            data.model_data.input.clear()
        return data.model_data

    await _run_with_inbox(
        Agent(name="other", model=other_model),
        inbox,
        streamed,
        value="other task",
        context=context,
        server_managed=True,
        run_config=RunConfig(call_model_input_filter=withhold_both),
    )

    assert other_model.calls[-1].input == []

    # The caller saves the paused run only after reusing the wrapper for another invocation.
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    state.add_input("first clarification")
    resumed = await _run_with_inbox(first, RunInbox(), streamed, value=state)
    assert resumed.final_output == "finished"
    assert effects == ["approved"]
    assert [
        item.get("content") for item in first_model.calls[-1].input if item.get("role") == "user"
    ] == ["first task", "first clarification"]
    assert hooks.end_inputs[-1] == [{"role": "user", "content": "first task"}]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
async def test_follow_up_turn_input_respects_handoff_filter(streamed: bool, filtered: bool) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()
    destination = Agent(
        name="destination", model=ScriptedModel([[get_text_message("done")]]), hooks=hooks
    )

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue("second task")
        return [get_text_message("first answer")]

    def filter_input(data: HandoffInputData) -> HandoffInputData:
        return replace(data, input_history="public summary", pre_handoff_items=())

    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(destination, args="{}")]]
        ),
        handoffs=[handoff(destination, input_filter=filter_input if filtered else None)],
    )
    await _run_with_inbox(source, inbox, streamed)
    content = "public summary" if filtered else "second task"
    expected = [[{"role": "user", "content": content}]]
    assert hooks.captured_turn_inputs == hooks.end_inputs == expected


@pytest.mark.asyncio
async def test_agent_hooks_receives_turn_input_string():
    """Test that on_start receives turn_input when input is a string."""
    hooks = AgentHooksWithTurnInput()
    model = ScriptedModel()
    agent = Agent(name="test", model=model, hooks=hooks)

    model.enqueue([get_text_message("response")])
    await Runner.run(agent, input="hello world")

    assert len(hooks.captured_turn_inputs) == 1
    turn_input = hooks.captured_turn_inputs[0]
    assert len(turn_input) == 1
    assert turn_input[0]["content"] == "hello world"
    assert turn_input[0]["role"] == "user"


@pytest.mark.asyncio
async def test_agent_hooks_receives_turn_input_list():
    """Test that on_start receives turn_input when input is a list."""
    hooks = AgentHooksWithTurnInput()
    model = ScriptedModel()
    agent = Agent(name="test", model=model, hooks=hooks)

    input_items: list[Any] = [
        {"role": "user", "content": "first message"},
        {"role": "user", "content": "second message"},
    ]

    model.enqueue([get_text_message("response")])
    await Runner.run(agent, input=input_items)

    assert len(hooks.captured_turn_inputs) == 1
    turn_input = hooks.captured_turn_inputs[0]
    assert len(turn_input) == 2
    assert turn_input[0]["content"] == "first message"
    assert turn_input[1]["content"] == "second message"


@pytest.mark.asyncio
async def test_agent_hooks_receives_turn_input_streamed():
    """Test that on_start receives turn_input in streamed mode."""
    hooks = AgentHooksWithTurnInput()
    model = ScriptedModel()
    agent = Agent(name="test", model=model, hooks=hooks)

    model.enqueue([get_text_message("response")])
    result = Runner.run_streamed(agent, input="streamed input")
    async for _ in result.stream_events():
        pass

    assert len(hooks.captured_turn_inputs) == 1
    turn_input = hooks.captured_turn_inputs[0]
    assert len(turn_input) == 1
    assert turn_input[0]["content"] == "streamed input"
