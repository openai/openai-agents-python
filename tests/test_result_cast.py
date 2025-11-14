import gc
import weakref
from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import BaseModel

from agents import Agent, MessageOutputItem, RunContextWrapper, RunResult
from agents.exceptions import AgentsException


def create_run_result(final_output: Any) -> RunResult:
    return RunResult(
        input="test",
        new_items=[],
        raw_responses=[],
        final_output=final_output,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        _last_agent=Agent(name="test"),
        context_wrapper=RunContextWrapper(context=None),
    )


class Foo(BaseModel):
    bar: int


def test_result_cast_typechecks():
    """Correct casts should work fine."""
    result = create_run_result(1)
    assert result.final_output_as(int) == 1

    result = create_run_result("test")
    assert result.final_output_as(str) == "test"

    result = create_run_result(Foo(bar=1))
    assert result.final_output_as(Foo) == Foo(bar=1)


def test_bad_cast_doesnt_raise():
    """Bad casts shouldn't error unless we ask for it."""
    result = create_run_result(1)
    result.final_output_as(str)

    result = create_run_result("test")
    result.final_output_as(Foo)


def test_bad_cast_with_param_raises():
    """Bad casts should raise a TypeError when we ask for it."""
    result = create_run_result(1)
    with pytest.raises(TypeError):
        result.final_output_as(str, raise_if_incorrect_type=True)

    result = create_run_result("test")
    with pytest.raises(TypeError):
        result.final_output_as(Foo, raise_if_incorrect_type=True)

    result = create_run_result(Foo(bar=1))
    with pytest.raises(TypeError):
        result.final_output_as(int, raise_if_incorrect_type=True)


def test_run_result_release_agents_breaks_strong_refs() -> None:
    message = ResponseOutputMessage(
        id="msg",
        content=[ResponseOutputText(annotations=[], text="hello", type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )
    agent = Agent(name="leak-test-agent")
    item = MessageOutputItem(agent=agent, raw_item=message)
    result = RunResult(
        input="test",
        new_items=[item],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        _last_agent=agent,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert item.agent is not None
    assert item.agent.name == "leak-test-agent"

    agent_ref = weakref.ref(agent)
    result.release_agents()
    del agent
    gc.collect()

    assert agent_ref() is None
    assert item.agent is None
    with pytest.raises(AgentsException):
        _ = result.last_agent
