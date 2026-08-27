from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from openai.types.responses import ResponseFunctionToolCall

from agents import Agent
from agents.items import MessageOutputItem, ToolCallOutputItem, TResponseInputItem
from agents.run_internal.approvals import (
    _build_function_tool_call_for_approval_error,
    append_approval_error_output,
    append_input_items_excluding_approvals,
    approvals_from_step,
    filter_tool_approvals,
)
from tests.utils.factories import make_message_output, make_tool_approval_item, make_tool_call


@dataclass
class _Step:
    interruptions: list[Any]


@dataclass
class _NoInterruptionsStep:
    value: str


class _NamespacedToolCall:
    namespace = "object_namespace"


def test_filter_tool_approvals_keeps_only_approval_items() -> None:
    agent = Agent(name="test")
    approval = make_tool_approval_item(agent)

    assert filter_tool_approvals(["text", approval, object()]) == [approval]


def test_approvals_from_step_handles_missing_and_mixed_interruptions() -> None:
    agent = Agent(name="test")
    approval = make_tool_approval_item(agent)

    assert approvals_from_step(_NoInterruptionsStep("none")) == []
    assert approvals_from_step(_Step(["other", approval])) == [approval]


def test_append_input_items_excluding_approvals_skips_approval_placeholders() -> None:
    agent = Agent(name="test")
    base_input: list[TResponseInputItem] = []
    message = MessageOutputItem(agent=agent, raw_item=make_message_output(text="done"))
    approval = make_tool_approval_item(agent, call_id="call_approval")

    append_input_items_excluding_approvals(base_input, [message, approval])

    assert len(base_input) == 1
    assert cast(dict[str, Any], base_input[0])["type"] == "message"


def test_append_approval_error_output_emits_function_tool_output() -> None:
    agent = Agent(name="test")
    generated_items: list[Any] = []

    append_approval_error_output(
        generated_items=generated_items,
        agent=agent,
        tool_call={"namespace": "dict_namespace"},
        tool_name="needs_approval",
        call_id=None,
        message="approval denied",
    )

    assert len(generated_items) == 1
    output_item = generated_items[0]
    assert isinstance(output_item, ToolCallOutputItem)
    assert output_item.agent is agent
    assert output_item.output == "approval denied"
    assert output_item.raw_item == {
        "type": "function_call_output",
        "call_id": "unknown",
        "output": "approval denied",
    }


def test_build_function_tool_call_for_approval_error_reuses_typed_calls() -> None:
    tool_call = make_tool_call(call_id="call_1", name="typed_tool")

    assert (
        _build_function_tool_call_for_approval_error(tool_call, "ignored", "ignored") is tool_call
    )


def test_build_function_tool_call_for_approval_error_preserves_namespace_sources() -> None:
    from_dict = _build_function_tool_call_for_approval_error(
        {"namespace": "dict_namespace"},
        "dict_tool",
        "call_dict",
    )
    from_object = _build_function_tool_call_for_approval_error(
        _NamespacedToolCall(),
        "object_tool",
        "call_object",
    )

    assert isinstance(from_dict, ResponseFunctionToolCall)
    assert from_dict.namespace == "dict_namespace"
    assert from_dict.call_id == "call_dict"
    assert from_object.namespace == "object_namespace"
    assert from_object.call_id == "call_object"


def test_build_function_tool_call_for_approval_error_ignores_empty_namespaces() -> None:
    tool_call = _build_function_tool_call_for_approval_error(
        {"namespace": ""},
        "tool",
        "call_1",
    )

    assert not hasattr(tool_call, "namespace") or tool_call.namespace is None
    assert tool_call.name == "tool"
    assert tool_call.arguments == "{}"
    assert tool_call.status == "completed"
