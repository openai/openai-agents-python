from __future__ import annotations

import json
from collections import defaultdict
from copy import copy
from dataclasses import replace
from typing import Any

import pytest
from typing_extensions import TypedDict

from agents.agent import Agent, AgentBase
from agents.exceptions import UserError
from agents.guardrail import GuardrailFunctionOutput, output_guardrail
from agents.handoffs import HandoffInputData, handoff, nest_handoff_history
from agents.items import InputItem, ItemHelpers, TResponseInputItem
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
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)

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
    value: str | list[TResponseInputItem] | RunState[Any] = "first task",
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
@pytest.mark.parametrize("serialize", [False, True], ids=["in-memory", "json"])
@pytest.mark.parametrize(
    "server_managed,omission",
    [(False, None), (True, None), (True, "all"), (True, "partial")],
    ids=["local", "server", "server-withheld", "server-partly-withheld"],
)
async def test_follow_up_turn_input_survives_approval_resume(
    streamed: bool, serialize: bool, server_managed: bool, omission: str | None
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
    if serialize:
        state = await RunState.from_string(agent, state.to_string())
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
@pytest.mark.parametrize(
    "filter_mode,serialize",
    [
        ("none", True),
        ("replace", True),
        ("untracked-summary", False),
        ("untracked-summary-shared", False),
        ("untracked-summary-copy", False),
        ("untracked-summary-nested-shared", False),
        ("untracked-summary-nested-copy", False),
        ("repeat", False),
        ("repeat", True),
        ("nest-equal-text", True),
        ("remove-tools-equal-text", True),
    ],
)
async def test_follow_up_turn_input_respects_handoff_filter(
    streamed: bool, filter_mode: str, serialize: bool
) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()
    tool_inputs: list[list[Any]] = []
    nested = filter_mode == "nest-equal-text"
    follow_up = "first task" if filter_mode.endswith("equal-text") else "second task"
    restriction = "Do not publish."
    if filter_mode.startswith("untracked-summary-"):
        follow_up = "Review the draft. Do not publish."

    @function_tool(needs_approval=True)
    async def work(ctx: RunContextWrapper[Any]) -> str:
        tool_inputs.append(list(ctx.turn_input))
        return "done"

    model = ScriptedModel([[get_function_tool_call("work", "{}")], [get_text_message("done")]])
    destination = Agent(name="destination", model=model, hooks=hooks, tools=[work])

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue(follow_up)
        return [get_text_message("first answer")]

    def filter_input(data: HandoffInputData) -> HandoffInputData:
        if filter_mode == "remove-tools-equal-text":
            from agents.extensions.handoff_filters import remove_all_tools

            return remove_all_tools(data)
        if filter_mode == "repeat":
            items = data.pre_handoff_items
            return data.clone(pre_handoff_items=(*items, items[-1]))
        if filter_mode == "untracked-summary":
            return data.clone(input_history="public summary", pre_handoff_items=())
        if filter_mode.startswith("untracked-summary-"):
            # The filter replaces both tasks with a summary of the follow-up.
            # Its wording matches the first task regardless of string allocation.
            summary = (
                restriction if filter_mode.endswith("shared") else restriction.encode().decode()
            )
            rewritten = data.clone(input_history=summary, pre_handoff_items=())
            if "nested" in filter_mode:
                return nest_handoff_history(rewritten, history_mapper=lambda rows: rows)
            return rewritten
        active = data.pre_handoff_items[-1]
        assert isinstance(active, InputItem)
        message: Any = active.raw_item
        message["content"] = "public summary"
        return data.clone(input_history=(message,), pre_handoff_items=())

    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(destination, args="{}")]]
        ),
        handoffs=[
            handoff(destination, nest_handoff_history=True)
            if nested
            else handoff(destination, input_filter=None if filter_mode == "none" else filter_input)
        ],
    )
    if filter_mode.startswith("untracked-summary"):
        initial = "first task" if filter_mode == "untracked-summary" else restriction
        with pytest.raises(UserError, match="Cannot identify active task input"):
            await _run_with_inbox(source, inbox, streamed, value=initial)
        assert not model.calls and not hooks.captured_turn_inputs and not tool_inputs
        return
    paused = await _run_with_inbox(source, inbox, streamed)
    content = "public summary" if filter_mode == "replace" else follow_up
    expected = [[{"role": "user", "content": content}]]
    assert hooks.captured_turn_inputs == expected
    assert len(paused.interruptions) == 1 and not tool_inputs
    state = paused.to_state()
    if serialize:
        context = RunContextWrapper(context=None)
        state = await RunState.from_string(source, state.to_string(), context_override=context)
        assert context.turn_input == expected[0]
    state.approve(state.get_interruptions()[0])
    resumed = await _run_with_inbox(source, RunInbox(), streamed, value=state)
    assert resumed.final_output == "done"
    assert tool_inputs == hooks.end_inputs == expected
    if nested:
        # The original request is in the assistant summary. Only the queued
        # occurrence is still a user message and identifies the active task.
        history = model.calls[0].input
        assert any(
            item.get("role") == "assistant" and "first task" in str(item.get("content"))
            for item in history
        )
        assert [item for item in history if item.get("role") == "user"] == expected[0]
    elif filter_mode == "replace":
        assert [
            item.get("content") for item in model.calls[-1].input if item.get("role") == "user"
        ] == ["public summary"]
    elif filter_mode == "repeat":
        assert [
            item.get("content") for item in model.calls[-1].input if item.get("role") == "user"
        ] == ["first task", "second task", "second task"]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialize", [False, True], ids=["in-memory", "json"])
@pytest.mark.parametrize(
    ("moved", "history_source"),
    [
        pytest.param(0, "distinct", id="move-first"),
        pytest.param(1, "distinct", id="move-second"),
        pytest.param(0, "split-call", id="resume-normalizes-history"),
        pytest.param(0, "active", id="move-repeated-active"),
        pytest.param(0, "earlier", id="retain-earlier-only"),
        pytest.param(0, "copy", id="ambiguous-copy"),
        pytest.param(0, "text", id="ambiguous-text"),
        pytest.param(0, "nested-text", id="ambiguous-nested-text"),
    ],
)
async def test_handoff_splits_follow_up_between_history_and_generated_items(
    streamed: bool, serialize: bool, moved: int, history_source: str
) -> None:
    inbox = RunInbox()
    restriction = "Do not publish."
    messages = ["Review the draft."]
    messages.insert(moved, restriction)
    checked: list[list[Any]] = []
    effects: list[str] = []

    def first_answer(call: ModelCall) -> list[Any]:
        for message in messages:
            inbox.steer(message)
        return [get_text_message("first answer")]

    def split(data: HandoffInputData) -> HandoffInputData:
        items = data.pre_handoff_items[-2:]
        item = items[moved]
        assert isinstance(item, InputItem)
        retained = item.raw_item
        if history_source == "split-call":
            # The destination receives both halves of the handoff. Resume normalizes
            # history separately, so the call before the restriction drops out.
            call, output = data.new_items
            return data.clone(
                input_history=(call.to_input_item(), retained),
                pre_handoff_items=(items[1 - moved],),
                input_items=(output,),
            )
        if history_source == "earlier":
            assert isinstance(data.input_history, tuple)
            retained = data.input_history[0]
        elif history_source == "copy":
            retained = copy(item.raw_item)
        elif history_source in ("text", "nested-text"):
            text = item.raw_item.get("content")
            assert isinstance(text, str)
            split_input = data.clone(input_history=text, pre_handoff_items=(items[1 - moved],))
            if history_source == "nested-text":
                return nest_handoff_history(split_input, history_mapper=lambda rows: rows)
            return split_input
        return data.clone(input_history=(retained,), pre_handoff_items=(items[1 - moved],))

    @tool_input_guardrail
    def check_task(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        texts = [item.get("content") for item in data.context.turn_input]
        checked.append(texts)
        if restriction in texts:
            return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")
        return ToolGuardrailFunctionOutput.allow()

    @function_tool(needs_approval=True, tool_input_guardrails=[check_task])
    async def publish() -> str:
        effects.append("published")
        return "done"

    model = ScriptedModel([[get_function_tool_call("publish", "{}")], [get_text_message("done")]])
    destination = Agent(name="destination", model=model, tools=[publish])
    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(destination, args="{}")]]
        ),
        handoffs=[handoff(destination, input_filter=split)],
    )
    initial: str | list[TResponseInputItem] = "first task"
    if history_source in ("text", "nested-text"):
        initial = restriction
    elif history_source not in ("distinct", "split-call"):
        initial = [{"role": "user", "content": restriction}]
    if history_source in ("copy", "text", "nested-text"):
        with pytest.raises(UserError, match="Cannot identify active task input"):
            await _run_with_inbox(source, inbox, streamed, value=initial)
        assert not model.calls
        assert checked == effects == []
        return

    paused = await _run_with_inbox(source, inbox, streamed, value=initial)
    assert [item.get("content") for item in model.calls[0].input if item.get("role") == "user"] == [
        restriction,
        "Review the draft.",
    ]
    state = paused.to_state()
    if serialize:
        state = await RunState.from_string(source, state.to_string())
    state.approve(state.get_interruptions()[0])
    await _run_with_inbox(source, RunInbox(), streamed, value=state)

    if history_source == "earlier":
        assert checked == [["Review the draft."]]
        assert effects == ["published"]
    else:
        assert effects == []
        assert checked == [[restriction, "Review the draft."]]
        assert model.calls[-1].input[-1]["output"] == "Publication blocked."


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialize", [False, True], ids=["in-memory", "json"])
@pytest.mark.parametrize("history_mode", ["keep", "trim", "add-summary"])
@pytest.mark.parametrize("drop_before_handoff", [False, True], ids=["direct", "after-drop"])
async def test_handoff_dropping_follow_up_does_not_reauthorize_original_task(
    streamed: bool, serialize: bool, history_mode: str, drop_before_handoff: bool
) -> None:
    inbox = RunInbox()
    hooks = AgentHooksWithTurnInput()
    checked: list[list[Any]] = []
    effects: list[str] = []
    initial: list[TResponseInputItem] = [
        {"role": "user", "content": "Earlier context."},
        {"role": "user", "content": "Publish the public draft."},
    ]
    summary: TResponseInputItem = {"role": "user", "content": "Review only. Do not publish."}
    expected_task = [summary] if history_mode == "add-summary" else []
    # Reformatting an earlier message as input_text does not make it part of the new task.
    reformatted: TResponseInputItem = {
        "role": "user",
        "content": [{"type": "input_text", "text": "Earlier context."}],
    }

    def public_task(context: RunContextWrapper[Any], agent: AgentBase[Any]) -> bool:
        return initial[-1] in context.turn_input

    @function_tool(is_enabled=public_task)
    async def original_only() -> str:
        effects.append("original_only")
        return "done"

    @tool_input_guardrail
    def check_task(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        checked.append(list(data.context.turn_input))
        if initial[-1] in data.context.turn_input or (
            history_mode == "add-summary" and summary not in data.context.turn_input
        ):
            return ToolGuardrailFunctionOutput.allow()
        return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")

    @function_tool(needs_approval=True, tool_input_guardrails=[check_task])
    async def publish() -> str:
        effects.append("publish")
        return "done"

    model = ScriptedModel([[get_function_tool_call("publish", "{}")], [get_text_message("done")]])
    destination = Agent(
        name="destination", model=model, hooks=hooks, tools=[original_only, publish]
    )

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue("Review the private draft.")
        return [get_text_message("first answer")]

    def filter_input(data: HandoffInputData) -> HandoffInputData:
        assert isinstance(data.input_history, tuple)
        history = data.input_history
        if history_mode == "trim":
            history = history[1:]
        elif history_mode == "add-summary":
            original: Any = history[0]
            original.update(reformatted)
            if drop_before_handoff:
                history = (*history, summary)
            else:
                active: Any = next(
                    item.raw_item for item in data.pre_handoff_items if isinstance(item, InputItem)
                )
                active.update(summary)
                history = (*history, active)
        return data.clone(input_history=history, pre_handoff_items=())

    def drop_follow_up(data: HandoffInputData) -> HandoffInputData:
        return data.clone(pre_handoff_items=())

    target = destination
    if drop_before_handoff:
        # The next filter can no longer identify the follow-up it wants to summarize.
        target = Agent(
            name="middle",
            model=ScriptedModel([[get_handoff_tool_call(destination, args="{}")]]),
            handoffs=[handoff(destination, input_filter=filter_input)],
        )

    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(target, args="{}")]]
        ),
        handoffs=[
            handoff(target, input_filter=drop_follow_up if drop_before_handoff else filter_input)
        ],
    )
    if drop_before_handoff and history_mode == "add-summary":
        with pytest.raises(UserError, match="Cannot identify active task input"):
            await _run_with_inbox(source, inbox, streamed, value=initial)
        assert not model.calls and not checked and not effects
        return
    paused = await _run_with_inbox(source, inbox, streamed, value=initial)
    expected_history = {
        "keep": initial,
        "trim": initial[1:],
        "add-summary": [reformatted, initial[-1], summary],
    }[history_mode]
    assert [item for item in model.calls[0].input if item.get("role") == "user"] == expected_history
    assert len(paused.interruptions) == 1 and checked == effects == []
    state = paused.to_state()
    if serialize:
        state = await RunState.from_string(source, state.to_string())
    state.approve(state.get_interruptions()[0])
    await _run_with_inbox(source, RunInbox(), streamed, value=state)

    assert effects == []
    assert hooks.captured_turn_inputs == [expected_task, expected_task]
    assert [[tool.name for tool in call.tools] for call in model.calls] == [
        ["publish"],
        ["publish"],
    ]
    assert checked == hooks.end_inputs == [expected_task]
    assert model.calls[-1].input[-1]["output"] == "Publication blocked."


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("path", "case"),
    [
        ("same-object", "edit-earlier"),
        ("same-object", "withdraw"),
        ("same-input-id", "edit-earlier"),
        ("same-input-id", "withdraw"),
        ("assistant-item", "withdraw"),
        ("assistant-history", "withdraw"),
        ("edit-before-conversion", "edit-earlier"),
        ("edit-after-conversion", "edit-earlier"),
        ("edit-after-conversion", "edit-active"),
        ("tracked-dict", "edit-active"),
        ("untracked-keep-earlier", "edit-active"),
        ("untracked-drop-earlier", "edit-active"),
        ("untracked-earlier", "edit-earlier"),
        ("tracked-edited-copy", "edit-earlier"),
        ("untracked-edited-copy", "edit-earlier"),
        ("rebuild-all", "unchanged"),
        ("rebuild-all", "edit-earlier"),
        ("normalize-history", "edit-earlier"),
        ("context-view", "edit-active"),
        *[
            (path, case)
            for path in ("config-mapper", "helper-mapper", "after-nesting")
            for case in ("edit-earlier", "edit-active", "equal")
        ],
    ],
)
async def test_handoff_keeps_earlier_permission_out_of_follow_up(
    streamed: bool, path: str, case: str
) -> None:
    inbox = RunInbox()
    restriction = "Do not publish."
    earlier = (
        restriction if case in ("edit-active", "equal") else "Publish the public draft for Alice."
    )
    edited = "Publish the public draft for [redacted]."
    edited_copy = path in ("tracked-edited-copy", "untracked-edited-copy")
    task = restriction if case == "equal" or edited_copy else "Review the private draft."
    quote_as_assistant = path in ("assistant-item", "assistant-history")
    if quote_as_assistant:
        task = edited
    keep_task = case != "withdraw"
    expected_earlier = edited if case in ("edit-earlier", "withdraw") else earlier
    expected_task = restriction if case == "edit-active" else task
    checked: list[list[Any]] = []
    effects: list[str] = []

    @tool_input_guardrail
    def check_task(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        texts = [item.get("content") for item in data.context.turn_input]
        checked.append(texts)
        # The policy must see the current restriction, never an earlier grant.
        if edited in texts or (case in ("edit-active", "equal") and restriction not in texts):
            return ToolGuardrailFunctionOutput.allow()
        return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")

    @function_tool(needs_approval=True, tool_input_guardrails=[check_task])
    async def publish() -> str:
        effects.append("published")
        return "done"

    model = ScriptedModel([[get_function_tool_call("publish", "{}")], [get_text_message("done")]])
    destination = Agent(name="destination", model=model, tools=[publish])

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue(earlier)
        inbox.queue(task)
        return [get_text_message("first answer")]

    def edit_messages(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
        users = [item for item in items if item.get("role") == "user"]
        # Keep the mapper's own objects, including when their text is identical.
        prior: Any = users[0]
        active: Any = users[-1]
        if case == "edit-earlier":
            prior["content"] = edited
        elif case == "edit-active":
            active["content"] = restriction
        return [prior, active]

    async def edit(data: HandoffInputData) -> HandoffInputData:
        if path == "helper-mapper":
            return nest_handoff_history(data, history_mapper=edit_messages)
        if path == "after-nesting":
            nested = nest_handoff_history(data)
            assert isinstance(nested.input_history, tuple)
            return nested.clone(input_history=tuple(edit_messages(list(nested.input_history))))
        prior, active = [item for item in data.pre_handoff_items if isinstance(item, InputItem)]
        if edited_copy:
            assert isinstance(data.input_history, tuple)
            message: Any = prior.raw_item
            message["content"] = edited
            duplicate = prior.to_input_item() if path == "tracked-edited-copy" else copy(message)
            return data.clone(
                input_history=(*data.input_history, message, duplicate),
                pre_handoff_items=(active,),
            )
        if path == "untracked-earlier":
            assert isinstance(data.input_history, tuple)
            rewritten: Any = copy(prior.raw_item)
            rewritten["content"] = edited
            return data.clone(
                input_history=(*data.input_history, rewritten), pre_handoff_items=(active,)
            )
        if path == "rebuild-all":
            assert isinstance(data.input_history, tuple)
            # Copy the entire user sequence. Editing just the earlier permission
            # loses its identity even though the active text stays unchanged.
            messages = edit_messages([copy(prior.raw_item), copy(active.raw_item)])
            history = (copy(data.input_history[0]), *messages)
            return data.clone(input_history=history, pre_handoff_items=())
        if path in ("tracked-dict", "untracked-keep-earlier", "untracked-drop-earlier"):
            # The same edit collides with the earlier text. Only the original
            # dictionary identifies which task the edited message belongs to.
            current = active.raw_item if path == "tracked-dict" else copy(active.raw_item)
            messages = edit_messages([prior.raw_item, current])
            if path == "untracked-drop-earlier":
                messages = messages[1:]
            return data.clone(input_history=tuple(messages), pre_handoff_items=())
        if path == "context-view":
            assert data.run_context is not None
            messages = edit_messages([prior.raw_item, data.run_context.turn_input[-1]])
            return data.clone(input_history=tuple(messages), pre_handoff_items=())
        retained = (active,) if keep_task else ()
        if path in ("same-object", "normalize-history"):
            assert isinstance(data.input_history, tuple)
            history = list(data.input_history)
            if path == "normalize-history":
                history = ItemHelpers.input_to_new_input_list(history)
            original: Any = history[0]
            original["content"] = edited
            return data.clone(input_history=tuple(history), pre_handoff_items=retained)
        if path == "edit-before-conversion":
            earlier_message: Any = prior.raw_item
            earlier_message["content"] = edited
            return data.clone(input_history=(prior.to_input_item(),), pre_handoff_items=retained)
        if path == "edit-after-conversion":
            if case == "edit-active":
                messages = edit_messages([prior.to_input_item(), active.to_input_item()])
                return data.clone(input_history=tuple(messages), pre_handoff_items=())
            converted: Any = prior.to_input_item()
            converted["content"] = edited
            return data.clone(input_history=(converted,), pre_handoff_items=retained)
        prior = replace(prior, raw_item={"role": "user", "content": edited})
        if quote_as_assistant:
            # Both filters quote the same text as an assistant message. Keeping
            # the InputItem ID must not keep the quote in the user's task.
            quote = replace(active, raw_item={"role": "assistant", "content": task})
            earlier_message = prior.to_input_item()
            if path == "assistant-history":
                return data.clone(
                    input_history=(earlier_message, quote.to_input_item()), pre_handoff_items=()
                )
            return data.clone(input_history=(earlier_message,), pre_handoff_items=(quote,))
        return data.clone(pre_handoff_items=(prior, *retained))

    source = Agent(
        name="source",
        model=ScriptedModel(
            [
                ModelStep.respond(first_answer),
                [get_text_message("second answer")],
                [get_handoff_tool_call(destination, args="{}")],
            ]
        ),
        handoffs=[
            handoff(destination, nest_handoff_history=True)
            if path == "config-mapper"
            else handoff(destination, input_filter=edit)
        ],
    )
    initial: list[TResponseInputItem] = [
        {
            "role": "user",
            "content": earlier
            if path in ("same-object", "normalize-history", "config-mapper", "helper-mapper")
            else "Start.",
        }
    ]
    config = RunConfig(handoff_history_mapper=edit_messages) if path == "config-mapper" else None
    if path.startswith("untracked-") or (path == "rebuild-all" and case == "edit-earlier"):
        with pytest.raises(UserError, match="Cannot identify active task input"):
            await _run_with_inbox(source, inbox, streamed, value=initial, run_config=config)
        assert not model.calls
        assert not checked and not effects
        return
    paused = await _run_with_inbox(source, inbox, streamed, value=initial, run_config=config)
    visible = [item.get("content") for item in model.calls[0].input if item.get("role") == "user"]
    expected_active = [expected_task] if keep_task else []
    if quote_as_assistant:
        assert [item for item in model.calls[0].input if item.get("role") == "assistant"] == [
            {"role": "assistant", "content": task}
        ]
    if path == "tracked-edited-copy":
        assert visible == ["Start.", edited, edited, restriction]
    else:
        assert visible == (["Start."] if path in ("same-input-id", "rebuild-all") else []) + [
            expected_earlier,
            *expected_active,
        ]
    state = await RunState.from_string(source, paused.to_state().to_string())
    state.approve(state.get_interruptions()[0])
    await _run_with_inbox(source, RunInbox(), streamed, value=state)

    assert effects == []
    assert checked == [expected_active]


@pytest.mark.parametrize("serialize", [False, True], ids=["in-memory", "json"])
async def test_failed_handoff_filter_preserves_source_task_on_resume(serialize: bool) -> None:
    inbox = RunInbox()
    checked: list[list[Any]] = []
    effects: list[str] = []

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue("Do not publish.")
        return [get_text_message("first answer")]

    def edit_then_fail(data: HandoffInputData) -> HandoffInputData:
        assert isinstance(data.input_history, tuple)
        original: Any = data.input_history[0]
        original["content"][0]["text"] = "Edited history."
        task = next(item for item in data.pre_handoff_items if isinstance(item, InputItem))
        message: Any = task.raw_item
        message["content"] = "Publish."
        raise ValueError("filter failed")

    @tool_input_guardrail
    def check_task(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        texts = [item.get("content") for item in data.context.turn_input]
        checked.append(texts)
        if "Publish." in texts:
            return ToolGuardrailFunctionOutput.allow()
        return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")

    @function_tool(tool_input_guardrails=[check_task])
    async def publish() -> str:
        effects.append("published")
        return "published"

    destination = Agent(name="destination", model=ScriptedModel([]))
    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [get_handoff_tool_call(destination, args="{}")],
            [get_function_tool_call("publish", "{}")],
            [get_text_message("done")],
        ]
    )
    source = Agent(
        name="source",
        model=model,
        tools=[publish],
        handoffs=[handoff(destination, input_filter=edit_then_fail)],
    )
    initial: list[TResponseInputItem] = [
        {"role": "user", "content": [{"type": "input_text", "text": "Review the draft."}]}
    ]
    stream = Runner.run_streamed(source, initial, inbox=inbox)
    with pytest.raises(ValueError, match="filter failed"):
        async for _ in stream.stream_events():
            pass

    state = stream.to_state()
    if serialize:
        state = await RunState.from_string(source, state.to_string())
    await Runner.run(source, state)

    assert effects == []
    assert checked == [["Do not publish."]]
    assert [item.get("content") for item in model.calls[2].input if item.get("role") == "user"] == [
        [{"type": "input_text", "text": "Review the draft."}],
        "Do not publish.",
    ]


@pytest.mark.asyncio
async def test_expanding_nested_history_keeps_quoted_permission_out_of_follow_up() -> None:
    inbox = RunInbox()
    permission = "Publish the public draft."
    task = "Review the private draft."
    hooks = AgentHooksWithTurnInput()

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue(task)
        return [get_text_message("Published the public draft.")]

    @function_tool(
        is_enabled=lambda context, agent: any(
            item.get("content") == permission for item in context.turn_input
        )
    )
    async def publish() -> str:
        return "published"

    model = ScriptedModel([[get_text_message("Reviewed.")]])
    destination = Agent(name="destination", model=model, hooks=hooks, tools=[publish])
    middle_model = ScriptedModel([[get_handoff_tool_call(destination, args="{}")]])
    middle = Agent(
        name="middle",
        model=middle_model,
        handoffs=[
            handoff(
                destination,
                input_filter=lambda data: nest_handoff_history(
                    data, history_mapper=lambda items: items
                ),
            )
        ],
    )
    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(middle, args="{}")]]
        ),
        handoffs=[handoff(middle, nest_handoff_history=True)],
    )
    await _run_with_inbox(source, inbox, False, value=permission)

    assert any(
        item.get("role") == "assistant" and permission in str(item.get("content"))
        for item in middle_model.calls[0].input
    )
    assert [item.get("content") for item in model.calls[0].input if item.get("role") == "user"] == [
        permission,
        task,
    ]
    assert hooks.captured_turn_inputs == [[{"role": "user", "content": task}]]
    assert not model.calls[0].tools


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("shared_id", [False, True], ids=["distinct-ids", "shared-id"])
async def test_handoff_keeps_identical_permissions_separate_when_follow_up_is_dropped(
    streamed: bool, shared_id: bool
) -> None:
    inbox = RunInbox()
    permission = "Publish."
    initial: list[TResponseInputItem] = [{"role": "user", "content": permission}]
    checked: list[list[TResponseInputItem]] = []
    effects: list[str] = []

    @tool_input_guardrail
    def check_task(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        checked.append(list(data.context.turn_input))
        if initial[0] in data.context.turn_input:
            return ToolGuardrailFunctionOutput.allow()
        return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")

    @function_tool(tool_input_guardrails=[check_task])
    async def publish() -> str:
        effects.append("published")
        return "done"

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue(permission)
        return [get_text_message("first answer")]

    def move_both(data: HandoffInputData) -> HandoffInputData:
        assert isinstance(data.input_history, tuple)
        active = next(item for item in data.pre_handoff_items if isinstance(item, InputItem))
        earlier = InputItem(agent=active.agent, raw_item=data.input_history[0])
        if shared_id:
            # Copying the wrapper keeps its ID even though the messages belong to different tasks.
            active = replace(earlier, raw_item=active.raw_item)
        return data.clone(input_history=(), pre_handoff_items=(earlier, active))

    def drop_active(data: HandoffInputData) -> HandoffInputData:
        return data.clone(pre_handoff_items=data.pre_handoff_items[:1])

    destination = Agent(
        name="destination",
        model=ScriptedModel(
            [[get_function_tool_call("publish", "{}")], [get_text_message("done")]]
        ),
        tools=[publish],
    )
    middle_model = ScriptedModel([[get_handoff_tool_call(destination, args="{}")]])
    middle = Agent(
        name="middle",
        model=middle_model,
        handoffs=[handoff(destination, input_filter=drop_active)],
    )
    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(middle, args="{}")]]
        ),
        handoffs=[handoff(middle, input_filter=move_both)],
    )

    if shared_id:
        with pytest.raises(UserError, match="active task input"):
            await _run_with_inbox(source, inbox, streamed, value=initial)
        assert not middle_model.calls and not checked
    else:
        await _run_with_inbox(source, inbox, streamed, value=initial)
        assert checked == [[]]
    assert not effects


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_follow_up_rejects_conflicting_handoff_copies_before_tool_execution(
    streamed: bool,
) -> None:
    inbox = RunInbox()
    effects: list[str] = []

    @function_tool(needs_approval=True)
    async def publish() -> str:
        effects.append("published")
        return "done"

    destination = Agent(
        name="destination",
        model=ScriptedModel(
            [[get_function_tool_call("publish", "{}")], [get_text_message("done")]]
        ),
        tools=[publish],
    )

    def first_answer(call: ModelCall) -> list[Any]:
        inbox.queue("private")
        return [get_text_message("first answer")]

    def filter_input(data: HandoffInputData) -> HandoffInputData:
        task = data.pre_handoff_items[-1]
        assert isinstance(task, InputItem)
        edited = replace(task, raw_item={"role": "user", "content": "public"})
        return data.clone(pre_handoff_items=(*data.pre_handoff_items, edited))

    source = Agent(
        name="source",
        model=ScriptedModel(
            [ModelStep.respond(first_answer), [get_handoff_tool_call(destination, args="{}")]]
        ),
        handoffs=[handoff(destination, input_filter=filter_input)],
    )
    with pytest.raises(UserError, match="Invalid active task input references"):
        paused = await _run_with_inbox(source, inbox, streamed, value="public")
        state = paused.to_state()
        state.approve(state.get_interruptions()[0])
        await _run_with_inbox(source, RunInbox(), streamed, value=state)
    assert effects == []


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
