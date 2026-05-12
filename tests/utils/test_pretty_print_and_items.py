from __future__ import annotations

from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import Agent
from agents.exceptions import RunErrorDetails
from agents.items import ItemHelpers, MessageOutputItem
from agents.util._pretty_print import pretty_print_run_error_details


def _make_message_item(text: str | None) -> MessageOutputItem:
    msg = ResponseOutputMessage.model_construct(
        id="msg_1",
        role="assistant",
        status="completed",
        content=[ResponseOutputText.model_construct(type="output_text", text=text, annotations=[])],
    )
    agent = Agent(name="test")
    return MessageOutputItem(agent=agent, raw_item=msg)


def test_text_message_output_returns_empty_string_for_none_text():
    """text_message_output must not crash when a content item has text=None."""
    item = _make_message_item(None)
    assert ItemHelpers.text_message_output(item) == ""


def test_text_message_output_returns_text_normally():
    item = _make_message_item("hello")
    assert ItemHelpers.text_message_output(item) == "hello"


def test_text_message_outputs_handles_none_text_across_items():
    """text_message_outputs must tolerate None text in any item."""
    from agents.items import RunItem

    items: list[RunItem] = [_make_message_item(None), _make_message_item("world")]
    assert ItemHelpers.text_message_outputs(items) == "world"


def _make_run_error_details(n_input: int = 0, n_output: int = 0) -> RunErrorDetails:
    return RunErrorDetails(
        input="hi",
        new_items=[],
        raw_responses=[],
        last_agent=Agent(name="test"),
        context_wrapper=None,  # type: ignore[arg-type]
        input_guardrail_results=[None] * n_input,  # type: ignore[list-item]
        output_guardrail_results=[None] * n_output,  # type: ignore[list-item]
    )


def test_pretty_print_run_error_details_includes_output_guardrail_count():
    """pretty_print_run_error_details must report output_guardrail_results like its siblings."""
    details = _make_run_error_details(n_input=1, n_output=2)
    text = pretty_print_run_error_details(details)
    assert "1 input guardrail result(s)" in text
    assert "2 output guardrail result(s)" in text


def test_pretty_print_run_error_details_zero_output_guardrails():
    details = _make_run_error_details(n_input=0, n_output=0)
    text = pretty_print_run_error_details(details)
    assert "0 output guardrail result(s)" in text
