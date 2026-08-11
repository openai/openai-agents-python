import pytest

from agents import Agent, run_demo_loop
from agents.testing import ScriptedModel

from .test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)


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
