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
