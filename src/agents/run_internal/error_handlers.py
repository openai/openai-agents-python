from __future__ import annotations

import inspect
import json
from typing import Any

from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from ..agent import Agent
from ..agent_output import _WRAPPER_DICT_KEY, AgentOutputSchema
from ..exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from ..items import (
    ItemHelpers,
    MessageOutputItem,
    ModelResponse,
    RunItem,
    ToolApprovalItem,
    TResponseInputItem,
)
from ..models.fake_id import FAKE_RESPONSES_ID
from ..run_context import RunContextWrapper, TContext
from ..run_error_handlers import (
    RunErrorData,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    RunErrorHandlers,
)
from .turn_preparation import get_output_schema


def build_run_error_data(
    *,
    input: str | list[TResponseInputItem],
    new_items: list[RunItem],
    raw_responses: list[ModelResponse],
    last_agent: Agent[Any],
) -> RunErrorData:
    history = ItemHelpers.input_to_new_input_list(input)
    output = []
    for item in new_items:
        if isinstance(item, ToolApprovalItem):
            continue
        output.append(item.to_input_item())
    history = history + list(output)
    return RunErrorData(
        input=input,
        new_items=list(new_items),
        history=history,
        output=output,
        raw_responses=list(raw_responses),
        last_agent=last_agent,
    )


def format_final_output_text(agent: Agent[Any], final_output: Any) -> str:
    output_schema = get_output_schema(agent)
    if output_schema is None or output_schema.is_plain_text():
        return str(final_output)
    payload_value = final_output
    if isinstance(output_schema, AgentOutputSchema) and output_schema._is_wrapped:
        if isinstance(final_output, dict) and _WRAPPER_DICT_KEY in final_output:
            payload_value = final_output
        else:
            payload_value = {_WRAPPER_DICT_KEY: final_output}
    try:
        if isinstance(output_schema, AgentOutputSchema):
            payload_bytes = output_schema._type_adapter.dump_json(payload_value)
            return (
                payload_bytes.decode()
                if isinstance(payload_bytes, (bytes, bytearray))
                else str(payload_bytes)
            )
        return json.dumps(payload_value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(final_output)


def validate_handler_final_output(agent: Agent[Any], final_output: Any) -> Any:
    output_schema = get_output_schema(agent)
    if output_schema is None or output_schema.is_plain_text():
        return final_output
    payload_value = final_output
    if isinstance(output_schema, AgentOutputSchema) and output_schema._is_wrapped:
        if isinstance(final_output, dict) and _WRAPPER_DICT_KEY in final_output:
            payload_value = final_output
        else:
            payload_value = {_WRAPPER_DICT_KEY: final_output}
    try:
        if isinstance(output_schema, AgentOutputSchema):
            payload_bytes = output_schema._type_adapter.dump_json(payload_value)
            payload = (
                payload_bytes.decode()
                if isinstance(payload_bytes, (bytes, bytearray))
                else str(payload_bytes)
            )
        else:
            payload = json.dumps(payload_value, ensure_ascii=False)
    except TypeError as exc:
        raise UserError("Invalid run error handler final_output for structured output.") from exc
    except ValueError as exc:
        raise UserError("Invalid run error handler final_output for structured output.") from exc
    try:
        return output_schema.validate_json(payload)
    except ModelBehaviorError as exc:
        raise UserError("Invalid run error handler final_output for structured output.") from exc


def create_message_output_item(agent: Agent[Any], output_text: str) -> MessageOutputItem:
    message = ResponseOutputMessage(
        id=FAKE_RESPONSES_ID,
        type="message",
        role="assistant",
        content=[
            ResponseOutputText(
                text=output_text,
                type="output_text",
                annotations=[],
                logprobs=[],
            )
        ],
        status="completed",
    )
    return MessageOutputItem(raw_item=message, agent=agent)


async def resolve_run_error_handler_result(
    *,
    error_handlers: RunErrorHandlers[TContext] | None,
    error: MaxTurnsExceeded,
    context_wrapper: RunContextWrapper[TContext],
    run_data: RunErrorData,
) -> RunErrorHandlerResult | None:
    if not error_handlers:
        return None
    handler = error_handlers.get("max_turns")
    if handler is None:
        return None
    handler_input = RunErrorHandlerInput(
        error=error,
        context=context_wrapper,
        run_data=run_data,
    )
    result = handler(handler_input)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return None
    if isinstance(result, RunErrorHandlerResult):
        return result
    if isinstance(result, dict):
        if "final_output" in result:
            allowed_keys = {"final_output", "include_in_history"}
            extra_keys = set(result.keys()) - allowed_keys
            if extra_keys:
                raise UserError("Invalid run error handler result.")
            try:
                return RunErrorHandlerResult(**result)
            except TypeError as exc:
                raise UserError("Invalid run error handler result.") from exc
        return RunErrorHandlerResult(final_output=result)
    return RunErrorHandlerResult(final_output=result)
