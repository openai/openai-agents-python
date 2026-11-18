import re

import pytest

from agents import Agent, RunContextWrapper, ShellTool, ToolSearchTool, handoff, run_demo_loop
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool import DEFAULT_APPROVAL_REJECTION_MESSAGE

from .test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)
from .utils.hitl import make_shell_call


@pytest.mark.asyncio
async def test_run_demo_loop_conversation(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("hello")], [get_text_message("good")]])

    agent = Agent(name="test", model=model)

    inputs = iter(["Hi", "How are you?", "quit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert "good" in output
    assert model.calls[-1].input == [
        get_text_input_item("Hi"),
        get_text_message("hello").model_dump(exclude_unset=True),
        get_text_input_item("How are you?"),
    ]


@pytest.mark.asyncio
async def test_run_demo_loop_streaming(monkeypatch, capsys):
    model = ScriptedModel()
    target_agent = Agent(name="target", model=model)
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
        handoffs=[target_agent],
    )

    # A single user turn that exercises every streamed event branch:
    # a tool call, the tool output, a handoff (agent update), then a text answer.
    model.extend(
        [
            [get_function_tool_call("foo", "{}")],
            [get_handoff_tool_call(target_agent)],
            [get_text_message("all done")],
        ]
    )

    inputs = iter(["Hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=True)

    output = capsys.readouterr().out
    assert "all done" in output
    assert "[tool called]" in output
    assert "[tool output: tool_result]" in output
    assert "[Agent updated: target]" in output


@pytest.mark.parametrize("stream", [False, True], ids=["nonstreaming", "streaming"])
@pytest.mark.asyncio
async def test_run_demo_loop_resolves_tool_approvals(monkeypatch, capsys, stream):
    recorded: list[str] = []

    @tool(needs_approval=True)
    def record(value: str) -> str:
        recorded.append(f"visible {value}")
        return f"visible {value}"

    @tool(name_override="record", defer_loading=True, needs_approval=True)
    def deferred_record(value: str) -> str:
        recorded.append(f"deferred {value}")
        return f"deferred {value}"

    done = assistant_message("All done")
    model = ScriptedModel(
        [
            [
                function_call("record", {"value": "alpha"}, call_id="call_1"),
                function_call("record", {"value": "alpha"}, call_id="call_2", namespace="record"),
            ],
            [function_call("record", {"value": "gamma"}, call_id="call_3", namespace="record")],
            [done],
            [assistant_message("You're welcome")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[record, deferred_record, ToolSearchTool()])
    inputs = iter(["Record these", "y", "", "yes", "Thanks", "quit"])
    prompts: list[str] = []

    def read_input(prompt: str) -> str:
        prompts.append(capsys.readouterr().out + prompt)
        return next(inputs)

    monkeypatch.setattr("builtins.input", read_input)
    await run_demo_loop(agent, stream=stream)

    assert recorded == ["visible alpha", "deferred gamma"]
    for prompt, value in zip(prompts[1:4], ["alpha", "alpha", "gamma"], strict=True):
        assert "record" in prompt and value in prompt
    namespace_label = re.compile(r"namespace\W+record\b", re.IGNORECASE)
    assert not namespace_label.search(prompts[1])
    assert namespace_label.search(prompts[2])
    assert len(model.calls) == 4
    assert all(call.streamed == stream for call in model.calls)
    _, first_resume, second_resume, follow_up = model.calls
    expected_outputs = [
        ("call_1", "visible alpha"),
        ("call_2", DEFAULT_APPROVAL_REJECTION_MESSAGE),
        ("call_3", "deferred gamma"),
    ]
    for call, expected in [(first_resume, expected_outputs[:2]), (second_resume, expected_outputs)]:
        assert [item for item in call.input if item.get("role") == "user"] == [
            get_text_input_item("Record these")
        ]
        assert (
            sorted(
                (item["call_id"], item["output"])
                for item in call.input
                if item.get("type") == "function_call_output"
            )
            == expected
        )
    assert follow_up.input == [
        *second_resume.input,
        done.model_dump(exclude_unset=True),
        get_text_input_item("Thanks"),
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["nonstreaming", "streaming"])
@pytest.mark.asyncio
async def test_run_demo_loop_resumes_with_handoff_context(monkeypatch, stream):
    def update_context(wrapper: RunContextWrapper[str]) -> None:
        wrapper.context = "after handoff"

    @tool(needs_approval=True)
    def read_context(wrapper: RunContextWrapper[str]) -> str:
        return wrapper.context

    model = ScriptedModel()
    target = Agent(name="target", model=model, tools=[read_context])
    source = Agent(
        name="source", model=model, handoffs=[handoff(target, on_handoff=update_context)]
    )
    model.extend(
        [
            [get_handoff_tool_call(target)],
            [function_call("read_context", {}, call_id="read")],
            [assistant_message("done")],
        ]
    )
    inputs = iter(["Start", "yes", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    await run_demo_loop(source, stream=stream, context="before handoff")

    assert [
        item["output"]
        for item in model.calls[-1].input
        if item.get("type") == "function_call_output" and item["call_id"] == "read"
    ] == ["after handoff"]


@pytest.mark.parametrize("stop", [EOFError, KeyboardInterrupt])
@pytest.mark.asyncio
async def test_run_demo_loop_exits_during_tool_approval(monkeypatch, capsys, stop):
    model = ScriptedModel(
        [
            [
                make_shell_call("call_1", commands=["echo alpha"]),
                make_shell_call("call_2", commands=["echo beta"]),
            ]
        ]
    )
    shell = ShellTool(
        executor=lambda _: pytest.fail("Unexpected shell execution"), needs_approval=True
    )
    agent = Agent(name="test", model=model, tools=[shell])
    inputs = iter(["Run these commands", "y"])

    def read_input(prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration:
            raise stop from None

    monkeypatch.setattr("builtins.input", read_input)
    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "echo alpha" in output and "echo beta" in output
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_run_demo_loop_exits_on_eof(monkeypatch, capsys):
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    def raise_eof(_=" > ") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    await run_demo_loop(agent, stream=False)

    # The loop should terminate cleanly without ever invoking the model.
    assert not model.calls


@pytest.mark.asyncio
async def test_run_demo_loop_skips_empty_input(monkeypatch, capsys):
    model = ScriptedModel()
    model.extend([[get_text_message("hello")]])
    agent = Agent(name="test", model=model)

    # Empty lines are ignored; only the non-empty input reaches the runner.
    inputs = iter(["", "Hi", "quit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert model.calls[-1].input == [get_text_input_item("Hi")]
