from __future__ import annotations

import dataclasses
import gc
import weakref
from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import BaseModel, ConfigDict

from agents import (
    Agent,
    MessageOutputItem,
    RunContextWrapper,
    RunItem,
    RunResult,
    RunResultStreaming,
)
from agents.exceptions import AgentsException


def create_run_result(
    final_output: Any | None,
    *,
    new_items: list[RunItem] | None = None,
    last_agent: Agent[Any] | None = None,
) -> RunResult:
    return RunResult(
        input="test",
        new_items=new_items or [],
        raw_responses=[],
        final_output=final_output,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        _last_agent=last_agent or Agent(name="test"),
        context_wrapper=RunContextWrapper(context=None),
        interruptions=[],
    )


class Foo(BaseModel):
    bar: int


def test_run_result_streaming_supports_pydantic_model_rebuild() -> None:
    class StreamingRunContainer(BaseModel):
        query_id: str
        run_stream: RunResultStreaming | None

        model_config = ConfigDict(arbitrary_types_allowed=True)

    StreamingRunContainer.model_rebuild()


def _create_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )


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
    message = _create_message("hello")
    agent = Agent(name="leak-test-agent")
    item = MessageOutputItem(agent=agent, raw_item=message)
    result = create_run_result(None, new_items=[item], last_agent=agent)
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


def test_run_item_retains_agent_when_result_is_garbage_collected() -> None:
    def build_item() -> tuple[MessageOutputItem, weakref.ReferenceType[RunResult]]:
        message = _create_message("persist")
        agent = Agent(name="persisted-agent")
        item = MessageOutputItem(agent=agent, raw_item=message)
        result = create_run_result(None, new_items=[item], last_agent=agent)
        return item, weakref.ref(result)

    item, result_ref = build_item()
    gc.collect()

    assert result_ref() is None
    assert item.agent is not None
    assert item.agent.name == "persisted-agent"


def test_run_item_repr_and_asdict_after_release() -> None:
    message = _create_message("repr")
    agent = Agent(name="repr-agent")
    item = MessageOutputItem(agent=agent, raw_item=message)

    item.release_agent()
    assert item.agent is agent

    text = repr(item)
    assert "MessageOutputItem" in text

    serialized = dataclasses.asdict(item)
    assert isinstance(serialized["agent"], dict)
    assert serialized["agent"]["name"] == "repr-agent"

    agent_ref = weakref.ref(agent)
    del agent
    gc.collect()

    assert agent_ref() is None
    assert item.agent is None

    serialized_after_gc = dataclasses.asdict(item)
    assert serialized_after_gc["agent"] is None


def test_run_result_repr_and_asdict_after_release_agents() -> None:
    agent = Agent(name="repr-result-agent")
    result = create_run_result(None, last_agent=agent)

    result.release_agents()

    text = repr(result)
    assert "RunResult" in text

    serialized = dataclasses.asdict(result)
    assert serialized["_last_agent"] is None


def test_run_result_release_agents_without_releasing_new_items() -> None:
    message = _create_message("keep")
    item_agent = Agent(name="item-agent")
    last_agent = Agent(name="last-agent")
    item = MessageOutputItem(agent=item_agent, raw_item=message)
    result = create_run_result(None, new_items=[item], last_agent=last_agent)

    result.release_agents(release_new_items=False)

    assert item.agent is item_agent

    last_agent_ref = weakref.ref(last_agent)
    del last_agent
    gc.collect()

    assert last_agent_ref() is None
    with pytest.raises(AgentsException):
        _ = result.last_agent


def test_run_result_release_agents_is_idempotent() -> None:
    message = _create_message("idempotent")
    agent = Agent(name="idempotent-agent")
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
        interruptions=[],
    )

    result.release_agents()
    result.release_agents()

    assert item.agent is agent

    agent_ref = weakref.ref(agent)
    del agent
    gc.collect()

    assert agent_ref() is None
    assert item.agent is None
    with pytest.raises(AgentsException):
        _ = result.last_agent


def test_run_result_streaming_release_agents_releases_current_agent() -> None:
    agent = Agent(name="streaming-agent")
    streaming_result = RunResultStreaming(
        input="stream",
        new_items=[],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(context=None),
        current_agent=agent,
        current_turn=0,
        max_turns=1,
        _current_agent_output_schema=None,
        trace=None,
        interruptions=[],
    )

    streaming_result.release_agents(release_new_items=False)

    agent_ref = weakref.ref(agent)
    del agent
    gc.collect()

    assert agent_ref() is None
    with pytest.raises(AgentsException):
        _ = streaming_result.last_agent
