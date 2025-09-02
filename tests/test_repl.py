import pytest

from agents import Agent, run_demo_loop

from .fake_model import FakeModel
from .test_responses import get_text_input_item, get_text_message


@pytest.mark.asyncio
async def test_run_demo_loop_conversation(monkeypatch, capsys):
    model = FakeModel()
    model.add_multiple_turn_outputs([[get_text_message("hello")], [get_text_message("good")]])

    agent = Agent(name="test", model=model)

    inputs = iter(["Hi", "How are you?", "quit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=False)

    output = capsys.readouterr().out
    assert "hello" in output
    assert "good" in output
    assert model.last_turn_args["input"] == [
        get_text_input_item("Hi"),
        get_text_message("hello").model_dump(exclude_unset=True),
        get_text_input_item("How are you?"),
    ]



@pytest.mark.asyncio
async def test_run_demo_loop_with_preload_history(monkeypatch, capsys):
    model = FakeModel()
    model.add_multiple_turn_outputs([[get_text_message("ready")]])

    agent = Agent(name="test", model=model)

    # Preload with a sample exchange
    preload_history = [
        {"role": "user", "content": "What's 2+2?"},
        {"role": "assistant", "content": "2+2 equals 4."},
    ]

    # User quits immediately (no extra turns)
    inputs = iter(["quit"])
    monkeypatch.setattr("builtins.input", lambda _=" > ": next(inputs))

    await run_demo_loop(agent, stream=False, preload_history=preload_history)

    output = capsys.readouterr().out
    # Verify that model saw the preload in its input history
    assert model.last_turn_args["input"][0]["content"] == "What's 2+2?"
    assert model.last_turn_args["input"][1]["content"] == "2+2 equals 4."
    # Verify model responded as configured
    assert "ready" in output
