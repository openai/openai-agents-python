from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
from collections.abc import AsyncGenerator, Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing_extensions import Literal, NotRequired, TypeAlias, TypedDict, TypeGuard

from agents import _debug
from agents.exceptions import ModelBehaviorError, UserError
from agents.logger import logger
from agents.models import _openai_shared
from agents.run_context import RunContextWrapper
from agents.strict_schema import ensure_strict_json_schema
from agents.tool import FunctionTool, ToolErrorFunction, default_tool_error_function
from agents.tool_context import ToolContext
from agents.tracing import SpanError, custom_span
from agents.usage import Usage as AgentsUsage
from agents.util import _error_tracing
from agents.util._types import MaybeAwaitable

from .codex import Codex
from .codex_options import CodexOptions, coerce_codex_options
from .events import (
    ItemCompletedEvent,
    ItemStartedEvent,
    ItemUpdatedEvent,
    ThreadErrorEvent,
    ThreadEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    Usage,
    coerce_thread_event,
)
from .items import (
    CommandExecutionItem,
    McpToolCallItem,
    ReasoningItem,
    ThreadItem,
    is_agent_message_item,
)
from .payloads import _DictLike
from .thread import Input, Thread, UserInput
from .thread_options import SandboxMode, ThreadOptions, coerce_thread_options
from .turn_options import TurnOptions, coerce_turn_options

JSON_PRIMITIVE_TYPES = {"string", "number", "integer", "boolean"}
SPAN_TRIM_KEYS = (
    "arguments",
    "command",
    "output",
    "result",
    "error",
    "text",
    "changes",
    "items",
)


class CodexToolInputItem(BaseModel):
    type: Literal["text", "local_image"]
    text: str | None = None
    path: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_item(self) -> CodexToolInputItem:
        text_value = (self.text or "").strip()
        path_value = (self.path or "").strip()

        if self.type == "text":
            if not text_value:
                raise ValueError('Text inputs must include a non-empty "text" field.')
            if path_value:
                raise ValueError('"path" is not allowed when type is "text".')
            self.text = text_value
            self.path = None
            return self

        if not path_value:
            raise ValueError('Local image inputs must include a non-empty "path" field.')
        if text_value:
            raise ValueError('"text" is not allowed when type is "local_image".')
        self.path = path_value
        self.text = None
        return self


class CodexToolParameters(BaseModel):
    inputs: list[CodexToolInputItem] = Field(
        ...,
        min_length=1,
        description=(
            "Structured inputs appended to the Codex task. Provide at least one input item."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class OutputSchemaPrimitive(TypedDict, total=False):
    type: Literal["string", "number", "integer", "boolean"]
    description: NotRequired[str]
    enum: NotRequired[list[str]]


class OutputSchemaArray(TypedDict, total=False):
    type: Literal["array"]
    description: NotRequired[str]
    items: OutputSchemaPrimitive


OutputSchemaField: TypeAlias = Union[OutputSchemaPrimitive, OutputSchemaArray]


class OutputSchemaPropertyDescriptor(TypedDict, total=False):
    name: str
    description: NotRequired[str]
    schema: OutputSchemaField


class OutputSchemaDescriptor(TypedDict, total=False):
    title: NotRequired[str]
    description: NotRequired[str]
    properties: list[OutputSchemaPropertyDescriptor]
    required: NotRequired[list[str]]


@dataclass(frozen=True)
class CodexToolResult:
    thread_id: str | None
    response: str
    usage: Usage | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "response": self.response,
            "usage": self.usage.as_dict() if isinstance(self.usage, Usage) else self.usage,
        }

    def __str__(self) -> str:
        return json.dumps(self.as_dict())


@dataclass(frozen=True)
class CodexToolStreamEvent(_DictLike):
    event: ThreadEvent
    thread: Thread
    tool_call: Any


@dataclass
class CodexToolOptions:
    name: str | None = None
    description: str | None = None
    parameters: type[BaseModel] | None = None
    output_schema: OutputSchemaDescriptor | Mapping[str, Any] | None = None
    codex: Codex | None = None
    codex_options: CodexOptions | Mapping[str, Any] | None = None
    default_thread_options: ThreadOptions | Mapping[str, Any] | None = None
    thread_id: str | None = None
    sandbox_mode: SandboxMode | None = None
    working_directory: str | None = None
    skip_git_repo_check: bool | None = None
    default_turn_options: TurnOptions | Mapping[str, Any] | None = None
    span_data_max_chars: int | None = 8192
    persist_session: bool = False
    on_stream: Callable[[CodexToolStreamEvent], MaybeAwaitable[None]] | None = None
    is_enabled: bool | Callable[[RunContextWrapper[Any], Any], MaybeAwaitable[bool]] = True
    failure_error_function: ToolErrorFunction | None = default_tool_error_function


CodexToolCallArguments: TypeAlias = dict[str, Optional[list[UserInput]]]


class _UnsetType:
    pass


_UNSET = _UnsetType()


def codex_tool(
    options: CodexToolOptions | Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: type[BaseModel] | None = None,
    output_schema: OutputSchemaDescriptor | Mapping[str, Any] | None = None,
    codex: Codex | None = None,
    codex_options: CodexOptions | Mapping[str, Any] | None = None,
    default_thread_options: ThreadOptions | Mapping[str, Any] | None = None,
    thread_id: str | None = None,
    sandbox_mode: SandboxMode | None = None,
    working_directory: str | None = None,
    skip_git_repo_check: bool | None = None,
    default_turn_options: TurnOptions | Mapping[str, Any] | None = None,
    span_data_max_chars: int | None | _UnsetType = _UNSET,
    persist_session: bool | None = None,
    on_stream: Callable[[CodexToolStreamEvent], MaybeAwaitable[None]] | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Any], MaybeAwaitable[bool]] | None = None,
    failure_error_function: ToolErrorFunction | None | _UnsetType = _UNSET,
) -> FunctionTool:
    resolved_options = _coerce_tool_options(options)
    if name is not None:
        resolved_options.name = name
    if description is not None:
        resolved_options.description = description
    if parameters is not None:
        resolved_options.parameters = parameters
    if output_schema is not None:
        resolved_options.output_schema = output_schema
    if codex is not None:
        resolved_options.codex = codex
    if codex_options is not None:
        resolved_options.codex_options = codex_options
    if default_thread_options is not None:
        resolved_options.default_thread_options = default_thread_options
    if thread_id is not None:
        resolved_options.thread_id = thread_id
    if sandbox_mode is not None:
        resolved_options.sandbox_mode = sandbox_mode
    if working_directory is not None:
        resolved_options.working_directory = working_directory
    if skip_git_repo_check is not None:
        resolved_options.skip_git_repo_check = skip_git_repo_check
    if default_turn_options is not None:
        resolved_options.default_turn_options = default_turn_options
    if not isinstance(span_data_max_chars, _UnsetType):
        resolved_options.span_data_max_chars = span_data_max_chars
    if persist_session is not None:
        resolved_options.persist_session = persist_session
    if on_stream is not None:
        resolved_options.on_stream = on_stream
    if is_enabled is not None:
        resolved_options.is_enabled = is_enabled
    if not isinstance(failure_error_function, _UnsetType):
        resolved_options.failure_error_function = failure_error_function

    resolved_options.codex_options = coerce_codex_options(resolved_options.codex_options)
    resolved_options.default_thread_options = coerce_thread_options(
        resolved_options.default_thread_options
    )
    resolved_options.default_turn_options = coerce_turn_options(
        resolved_options.default_turn_options
    )
    name = resolved_options.name or "codex"
    description = resolved_options.description or (
        "Executes an agentic Codex task against the current workspace."
    )
    parameters_model = resolved_options.parameters or CodexToolParameters

    params_schema = ensure_strict_json_schema(parameters_model.model_json_schema())
    resolved_codex_options = _resolve_codex_options(resolved_options.codex_options)
    resolve_codex = _create_codex_resolver(resolved_options.codex, resolved_codex_options)

    validated_output_schema = _resolve_output_schema(resolved_options.output_schema)
    resolved_thread_options = _resolve_thread_options(
        resolved_options.default_thread_options,
        resolved_options.sandbox_mode,
        resolved_options.working_directory,
        resolved_options.skip_git_repo_check,
    )

    persisted_thread: Thread | None = None

    async def _on_invoke_tool(ctx: ToolContext[Any], input_json: str) -> Any:
        nonlocal persisted_thread
        try:
            parsed = _parse_tool_input(parameters_model, input_json)
            args = _normalize_parameters(parsed)

            codex = await resolve_codex()
            if resolved_options.persist_session:
                # Reuse a single Codex thread across tool calls.
                thread = _get_or_create_persisted_thread(
                    codex,
                    resolved_options.thread_id,
                    resolved_thread_options,
                    persisted_thread,
                )
                if persisted_thread is None:
                    persisted_thread = thread
            else:
                thread = _get_thread(codex, resolved_options.thread_id, resolved_thread_options)

            turn_options = _build_turn_options(
                resolved_options.default_turn_options, validated_output_schema
            )
            codex_input = _build_codex_input(args)

            # Always stream and aggregate locally to enable on_stream callbacks.
            stream_result = await thread.run_streamed(codex_input, turn_options)
            response, usage = await _consume_events(
                stream_result.events,
                args,
                ctx,
                thread,
                resolved_options.on_stream,
                resolved_options.span_data_max_chars,
            )

            if usage is not None:
                ctx.usage.add(_to_agent_usage(usage))

            return CodexToolResult(thread_id=thread.id, response=response, usage=usage)
        except Exception as exc:  # noqa: BLE001
            if resolved_options.failure_error_function is None:
                raise

            result = resolved_options.failure_error_function(ctx, exc)
            if inspect.isawaitable(result):
                result = await result

            _error_tracing.attach_error_to_current_span(
                SpanError(
                    message="Error running Codex tool (non-fatal)",
                    data={"tool_name": name, "error": str(exc)},
                )
            )
            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug("Codex tool failed")
            else:
                logger.error("Codex tool failed: %s", exc, exc_info=exc)
            return result

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=params_schema,
        on_invoke_tool=_on_invoke_tool,
        strict_json_schema=True,
        is_enabled=resolved_options.is_enabled,
    )


def _coerce_tool_options(
    options: CodexToolOptions | Mapping[str, Any] | None,
) -> CodexToolOptions:
    if options is None:
        return CodexToolOptions()
    if isinstance(options, CodexToolOptions):
        resolved = options
    else:
        if not isinstance(options, Mapping):
            raise UserError("Codex tool options must be a CodexToolOptions or a mapping.")

        allowed = {field.name for field in dataclasses.fields(CodexToolOptions)}
        unknown = set(options.keys()) - allowed
        if unknown:
            raise UserError(f"Unknown Codex tool option(s): {sorted(unknown)}")

        resolved = CodexToolOptions(**dict(options))
    # Normalize nested option dictionaries to their dataclass equivalents.
    resolved.codex_options = coerce_codex_options(resolved.codex_options)
    resolved.default_thread_options = coerce_thread_options(resolved.default_thread_options)
    resolved.default_turn_options = coerce_turn_options(resolved.default_turn_options)
    return resolved


def _parse_tool_input(parameters_model: type[BaseModel], input_json: str) -> BaseModel:
    try:
        json_data = json.loads(input_json) if input_json else {}
    except Exception as exc:  # noqa: BLE001
        if _debug.DONT_LOG_TOOL_DATA:
            logger.debug("Invalid JSON input for codex tool")
        else:
            logger.debug("Invalid JSON input for codex tool: %s", input_json)
        raise ModelBehaviorError(f"Invalid JSON input for codex tool: {input_json}") from exc

    try:
        return parameters_model.model_validate(json_data)
    except ValidationError as exc:
        raise ModelBehaviorError(f"Invalid JSON input for codex tool: {exc}") from exc


def _normalize_parameters(params: BaseModel) -> CodexToolCallArguments:
    inputs_value = getattr(params, "inputs", None)
    if inputs_value is None:
        raise UserError("Codex tool parameters must include an inputs field.")

    inputs = [{"type": item.type, "text": item.text, "path": item.path} for item in inputs_value]

    normalized_inputs: list[UserInput] = []
    for item in inputs:
        if item["type"] == "text":
            normalized_inputs.append({"type": "text", "text": item["text"] or ""})
        else:
            normalized_inputs.append({"type": "local_image", "path": item["path"] or ""})

    return {"inputs": normalized_inputs if normalized_inputs else None}


def _build_codex_input(args: CodexToolCallArguments) -> Input:
    if args.get("inputs"):
        return args["inputs"]  # type: ignore[return-value]
    return ""


def _resolve_codex_options(
    options: CodexOptions | Mapping[str, Any] | None,
) -> CodexOptions | None:
    options = coerce_codex_options(options)
    if options and options.api_key:
        return options

    api_key = _resolve_default_codex_api_key(options)
    if not api_key:
        return options

    if options is None:
        return CodexOptions(api_key=api_key)

    return CodexOptions(
        codex_path_override=options.codex_path_override,
        base_url=options.base_url,
        api_key=api_key,
        env=options.env,
    )


def _resolve_default_codex_api_key(options: CodexOptions | None) -> str | None:
    if options and options.api_key:
        return options.api_key

    env_override = options.env if options else None
    if env_override:
        env_codex = env_override.get("CODEX_API_KEY")
        if env_codex:
            return env_codex
        env_openai = env_override.get("OPENAI_API_KEY")
        if env_openai:
            return env_openai

    env_codex = os.environ.get("CODEX_API_KEY")
    if env_codex:
        return env_codex

    env_openai = os.environ.get("OPENAI_API_KEY")
    if env_openai:
        return env_openai

    return _openai_shared.get_default_openai_key()


def _create_codex_resolver(
    provided: Codex | None, options: CodexOptions | None
) -> Callable[[], Awaitable[Codex]]:
    if provided is not None:

        async def _return_provided() -> Codex:
            return provided

        return _return_provided

    codex_instance: Codex | None = None

    async def _get_or_create() -> Codex:
        nonlocal codex_instance
        if codex_instance is None:
            codex_instance = Codex(options)
        return codex_instance

    return _get_or_create


def _resolve_thread_options(
    defaults: ThreadOptions | Mapping[str, Any] | None,
    sandbox_mode: SandboxMode | None,
    working_directory: str | None,
    skip_git_repo_check: bool | None,
) -> ThreadOptions | None:
    defaults = coerce_thread_options(defaults)
    if not defaults and not sandbox_mode and not working_directory and skip_git_repo_check is None:
        return None

    return ThreadOptions(
        **{
            **(defaults.__dict__ if defaults else {}),
            **({"sandbox_mode": sandbox_mode} if sandbox_mode else {}),
            **({"working_directory": working_directory} if working_directory else {}),
            **(
                {"skip_git_repo_check": skip_git_repo_check}
                if skip_git_repo_check is not None
                else {}
            ),
        }
    )


def _build_turn_options(
    defaults: TurnOptions | Mapping[str, Any] | None,
    output_schema: dict[str, Any] | None,
) -> TurnOptions:
    defaults = coerce_turn_options(defaults)
    if defaults is None and output_schema is None:
        return TurnOptions()

    if defaults is None:
        return TurnOptions(output_schema=output_schema, signal=None, idle_timeout_seconds=None)

    merged_output_schema = output_schema if output_schema is not None else defaults.output_schema
    return TurnOptions(
        output_schema=merged_output_schema,
        signal=defaults.signal,
        idle_timeout_seconds=defaults.idle_timeout_seconds,
    )


def _resolve_output_schema(
    option: OutputSchemaDescriptor | Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if option is None:
        return None

    if isinstance(option, Mapping) and _looks_like_descriptor(option):
        # Descriptor input is converted to a strict JSON schema for Codex.
        descriptor = _validate_descriptor(option)
        return _build_codex_output_schema(descriptor)

    if isinstance(option, Mapping):
        schema = dict(option)
        if "type" in schema and schema.get("type") != "object":
            raise UserError('Codex output schema must be a JSON object schema with type "object".')
        return ensure_strict_json_schema(schema)

    raise UserError("Codex output schema must be a JSON schema or descriptor.")


def _looks_like_descriptor(option: Mapping[str, Any]) -> bool:
    properties = option.get("properties")
    if not isinstance(properties, list):
        return False
    return all(isinstance(item, Mapping) and "name" in item for item in properties)


def _validate_descriptor(option: Mapping[str, Any]) -> OutputSchemaDescriptor:
    properties = option.get("properties")
    if not isinstance(properties, list) or not properties:
        raise UserError("Codex output schema descriptor must include properties.")

    seen: set[str] = set()
    for prop in properties:
        name = prop.get("name") if isinstance(prop, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            raise UserError("Codex output schema properties must include non-empty names.")
        if name in seen:
            raise UserError(f'Duplicate property name "{name}" in output_schema.')
        seen.add(name)

        schema = prop.get("schema")
        if not _is_valid_field(schema):
            raise UserError(f'Invalid schema for output property "{name}".')

    required = option.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise UserError("output_schema.required must be a list of strings.")
        for name in required:
            if name not in seen:
                raise UserError(f'Required property "{name}" must also be defined in "properties".')

    return option  # type: ignore[return-value]


def _is_valid_field(field: Any) -> bool:
    if not isinstance(field, Mapping):
        return False
    field_type = field.get("type")
    if field_type in JSON_PRIMITIVE_TYPES:
        enum = field.get("enum")
        if enum is not None and (
            not isinstance(enum, list) or not all(isinstance(item, str) for item in enum)
        ):
            return False
        return True
    if field_type == "array":
        items = field.get("items")
        return _is_valid_field(items)
    return False


def _build_codex_output_schema(descriptor: OutputSchemaDescriptor) -> dict[str, Any]:
    # Compose the strict object schema required by Codex structured outputs.
    properties: dict[str, Any] = {}
    for prop in descriptor["properties"]:
        prop_schema = _build_codex_output_schema_field(prop["schema"])
        if prop.get("description"):
            prop_schema["description"] = prop["description"]
        properties[prop["name"]] = prop_schema

    required = list(descriptor.get("required", []))

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }

    if "title" in descriptor and descriptor["title"]:
        schema["title"] = descriptor["title"]
    if "description" in descriptor and descriptor["description"]:
        schema["description"] = descriptor["description"]

    return schema


def _build_codex_output_schema_field(field: OutputSchemaField) -> dict[str, Any]:
    if field["type"] == "array":
        schema: dict[str, Any] = {
            "type": "array",
            "items": _build_codex_output_schema_field(field["items"]),
        }
        if "description" in field and field["description"]:
            schema["description"] = field["description"]
        return schema
    result: dict[str, Any] = {"type": field["type"]}
    if "description" in field and field["description"]:
        result["description"] = field["description"]
    if "enum" in field:
        result["enum"] = field["enum"]
    return result


def _get_thread(codex: Codex, thread_id: str | None, defaults: ThreadOptions | None) -> Thread:
    if thread_id:
        return codex.resume_thread(thread_id, defaults)
    return codex.start_thread(defaults)


def _get_or_create_persisted_thread(
    codex: Codex,
    thread_id: str | None,
    thread_options: ThreadOptions | None,
    existing_thread: Thread | None,
) -> Thread:
    if existing_thread is not None:
        if thread_id:
            existing_id = existing_thread.id
            if existing_id and existing_id != thread_id:
                raise UserError(
                    "Codex tool is configured with persist_session=true "
                    + "and already has an active thread."
                )
        return existing_thread

    return _get_thread(codex, thread_id, thread_options)


def _to_agent_usage(usage: Usage) -> AgentsUsage:
    return AgentsUsage(
        requests=1,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.input_tokens + usage.output_tokens,
        input_tokens_details=InputTokensDetails(cached_tokens=usage.cached_input_tokens),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )


async def _consume_events(
    events: AsyncGenerator[ThreadEvent | Mapping[str, Any], None],
    args: CodexToolCallArguments,
    ctx: ToolContext[Any],
    thread: Thread,
    on_stream: Callable[[CodexToolStreamEvent], MaybeAwaitable[None]] | None,
    span_data_max_chars: int | None,
) -> tuple[str, Usage | None]:
    # Track spans keyed by item id for command/mcp/reasoning events.
    active_spans: dict[str, Any] = {}
    final_response = ""
    usage: Usage | None = None

    event_queue: asyncio.Queue[CodexToolStreamEvent | None] | None = None
    dispatch_task: asyncio.Task[None] | None = None

    if on_stream is not None:
        # Buffer events so user callbacks cannot block the Codex stream loop.
        event_queue = asyncio.Queue()

        async def _run_handler(payload: CodexToolStreamEvent) -> None:
            # Dispatch user callbacks asynchronously to avoid blocking the stream.
            try:
                maybe_result = on_stream(payload)
                if inspect.isawaitable(maybe_result):
                    await maybe_result
            except Exception:
                logger.exception("Error while handling Codex on_stream event.")

        async def _dispatch() -> None:
            assert event_queue is not None
            while True:
                payload = await event_queue.get()
                is_sentinel = payload is None
                try:
                    if payload is not None:
                        await _run_handler(payload)
                finally:
                    event_queue.task_done()
                if is_sentinel:
                    break

        dispatch_task = asyncio.create_task(_dispatch())

    try:
        async for raw_event in events:
            event = coerce_thread_event(raw_event)
            if event_queue is not None:
                await event_queue.put(
                    CodexToolStreamEvent(
                        event=event,
                        thread=thread,
                        tool_call=ctx.tool_call,
                    )
                )

            if isinstance(event, ItemStartedEvent):
                _handle_item_started(event.item, active_spans, span_data_max_chars)
            elif isinstance(event, ItemUpdatedEvent):
                _handle_item_updated(event.item, active_spans, span_data_max_chars)
            elif isinstance(event, ItemCompletedEvent):
                _handle_item_completed(event.item, active_spans, span_data_max_chars)
                if is_agent_message_item(event.item):
                    final_response = event.item.text
            elif isinstance(event, TurnCompletedEvent):
                usage = event.usage
            elif isinstance(event, TurnFailedEvent):
                error = event.error.message
                raise UserError(f"Codex turn failed{(': ' + error) if error else ''}")
            elif isinstance(event, ThreadErrorEvent):
                raise UserError(f"Codex stream error: {event.message}")
    finally:
        if event_queue is not None:
            await event_queue.put(None)
            await event_queue.join()
        if dispatch_task is not None:
            await dispatch_task

        # Ensure any open spans are closed even on failure.
        for span in active_spans.values():
            span.finish()
        active_spans.clear()

    if not final_response:
        final_response = _build_default_response(args)

    return final_response, usage


def _handle_item_started(
    item: ThreadItem, spans: dict[str, Any], span_data_max_chars: int | None
) -> None:
    item_id = getattr(item, "id", None)
    if not item_id:
        return

    if _is_command_execution_item(item):
        output = item.aggregated_output
        updates = {
            "command": item.command,
            "status": item.status,
            "exit_code": item.exit_code,
        }
        if output not in (None, ""):
            updates["output"] = _truncate_span_value(output, span_data_max_chars)
        data = _merge_span_data(
            {},
            updates,
            span_data_max_chars,
        )
        span = custom_span(
            name="Codex command execution",
            data=data,
        )
        span.start()
        spans[item_id] = span
        return

    if _is_mcp_tool_call_item(item):
        data = _merge_span_data(
            {},
            {
                "server": item.server,
                "tool": item.tool,
                "status": item.status,
                "arguments": _truncate_span_value(
                    _maybe_as_dict(item.arguments), span_data_max_chars
                ),
            },
            span_data_max_chars,
        )
        span = custom_span(
            name="Codex MCP tool call",
            data=data,
        )
        span.start()
        spans[item_id] = span
        return

    if _is_reasoning_item(item):
        data = _merge_span_data(
            {},
            {"text": _truncate_span_value(item.text, span_data_max_chars)},
            span_data_max_chars,
        )
        span = custom_span(
            name="Codex reasoning",
            data=data,
        )
        span.start()
        spans[item_id] = span


def _handle_item_updated(
    item: ThreadItem, spans: dict[str, Any], span_data_max_chars: int | None
) -> None:
    item_id = getattr(item, "id", None)
    if not item_id:
        return
    span = spans.get(item_id)
    if span is None:
        return

    if _is_command_execution_item(item):
        _update_command_span(span, item, span_data_max_chars)
    elif _is_mcp_tool_call_item(item):
        _update_mcp_tool_span(span, item, span_data_max_chars)
    elif _is_reasoning_item(item):
        _update_reasoning_span(span, item, span_data_max_chars)


def _handle_item_completed(
    item: ThreadItem, spans: dict[str, Any], span_data_max_chars: int | None
) -> None:
    item_id = getattr(item, "id", None)
    if not item_id:
        return
    span = spans.get(item_id)
    if span is None:
        return

    if _is_command_execution_item(item):
        _update_command_span(span, item, span_data_max_chars)
        if item.status == "failed":
            error_data: dict[str, Any] = {
                "exit_code": item.exit_code,
            }
            output = item.aggregated_output
            if output not in (None, ""):
                error_data["output"] = _truncate_span_value(output, span_data_max_chars)
            span.set_error(
                SpanError(
                    message="Codex command execution failed.",
                    data=error_data,
                )
            )
    elif _is_mcp_tool_call_item(item):
        _update_mcp_tool_span(span, item, span_data_max_chars)
        error = item.error
        if item.status == "failed" and error is not None and error.message:
            span.set_error(SpanError(message=error.message, data={}))
    elif _is_reasoning_item(item):
        _update_reasoning_span(span, item, span_data_max_chars)

    span.finish()
    spans.pop(item_id, None)


def _truncate_span_string(value: str, max_chars: int | None) -> str:
    if max_chars is None:
        return value
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value

    suffix = f"... [truncated, {len(value)} chars]"
    max_prefix = max_chars - len(suffix)
    if max_prefix <= 0:
        return value[:max_chars]
    return value[:max_prefix] + suffix


def _json_char_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str))
    except Exception:
        return len(str(value))


def _drop_empty_string_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value != ""}


def _stringify_span_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def _maybe_as_dict(value: Any) -> Any:
    if isinstance(value, _DictLike):
        return value.as_dict()
    if isinstance(value, list):
        return [_maybe_as_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: _maybe_as_dict(item) for key, item in value.items()}
    return value


def _truncate_span_value(value: Any, max_chars: int | None) -> Any:
    if max_chars is None:
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_span_string(value, max_chars)

    try:
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
    except Exception:
        encoded = str(value)

    if len(encoded) <= max_chars:
        return value

    return {
        "preview": _truncate_span_string(encoded, max_chars),
        "truncated": True,
        "original_length": len(encoded),
    }


def _enforce_span_data_budget(data: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    # Trim span payloads to fit the overall JSON size budget while preserving keys.
    if max_chars is None:
        return _drop_empty_string_fields(data)
    if max_chars <= 0:
        return {}

    trimmed = _drop_empty_string_fields(dict(data))
    if _json_char_size(trimmed) <= max_chars:
        return trimmed

    trim_keys = SPAN_TRIM_KEYS
    kept_keys = [key for key in trim_keys if key in trimmed]
    if not kept_keys:
        return trimmed

    base = dict(trimmed)
    for key in kept_keys:
        base[key] = ""
    base_size = _json_char_size(base)

    while base_size > max_chars and kept_keys:
        # Drop lowest-priority keys only if the empty base cannot fit.
        drop_key = kept_keys.pop()
        base.pop(drop_key, None)
        trimmed.pop(drop_key, None)
        base_size = _json_char_size(base)

    if base_size > max_chars:
        return _drop_empty_string_fields(base)

    values = {
        key: _stringify_span_value(trimmed[key])
        for key in kept_keys
        if trimmed.get(key) not in ("", None)
    }
    for key, value in list(values.items()):
        if value == "":
            values.pop(key, None)
            trimmed[key] = ""
    kept_keys = [key for key in kept_keys if key in values or key in trimmed]

    if not kept_keys:
        return _drop_empty_string_fields(base)

    base_size = _json_char_size(base)
    available = max_chars - base_size
    if available <= 0:
        return _drop_empty_string_fields(base)

    ordered_keys = [key for key in trim_keys if key in values]
    min_budget = 1
    budgets = {key: 0 for key in values}
    if available >= len(values):
        for key in values:
            budgets[key] = min_budget
        remaining = available - len(values)
    else:
        for key in ordered_keys[:available]:
            budgets[key] = min_budget
        remaining = 0

    if "arguments" in values and remaining > 0:
        # Keep arguments intact when they already fit within the budget.
        needed = len(values["arguments"]) - budgets["arguments"]
        if needed > 0:
            grant = min(needed, remaining)
            budgets["arguments"] += grant
            remaining -= grant

    if remaining > 0:
        weights = {key: max(len(values[key]) - budgets[key], 0) for key in values}
        weight_total = sum(weights.values())
        if weight_total > 0:
            for key, weight in weights.items():
                if weight == 0:
                    continue
                budgets[key] += int(remaining * (weight / weight_total))
        for key in list(budgets.keys()):
            budgets[key] = min(budgets[key], len(values[key]))
        allocated = sum(budgets.values())
        leftover = available - allocated
        if leftover > 0:
            ordered = sorted(values.keys(), key=lambda k: weights.get(k, 0), reverse=True)
            idx = 0
            while leftover > 0:
                expandable = [key for key in ordered if budgets[key] < len(values[key])]
                if not expandable:
                    break
                key = expandable[idx % len(expandable)]
                budgets[key] += 1
                leftover -= 1
                idx += 1

    for key in kept_keys:
        if key in values:
            trimmed[key] = _truncate_span_string(values[key], budgets.get(key, 0))
        else:
            trimmed[key] = ""

    size = _json_char_size(trimmed)
    while size > max_chars and kept_keys:
        key = max(kept_keys, key=lambda k: len(str(trimmed.get(k, ""))))
        current = str(trimmed.get(key, ""))
        if len(current) > 0:
            trimmed[key] = _truncate_span_string(values.get(key, ""), len(current) - 1)
        else:
            kept_keys.remove(key)
        size = _json_char_size(trimmed)

    if _json_char_size(trimmed) <= max_chars:
        return _drop_empty_string_fields(trimmed)
    return _drop_empty_string_fields(base)


def _merge_span_data(
    current: dict[str, Any],
    updates: dict[str, Any],
    max_chars: int | None,
) -> dict[str, Any]:
    merged = {**current, **updates}
    return _enforce_span_data_budget(merged, max_chars)


def _apply_span_updates(
    span: Any,
    updates: dict[str, Any],
    max_chars: int | None,
) -> None:
    # Update span data in place to keep references stable for tracing processors.
    current = span.span_data.data
    trimmed = _merge_span_data(current, updates, max_chars)
    current.clear()
    current.update(trimmed)


def _update_command_span(
    span: Any, item: CommandExecutionItem, span_data_max_chars: int | None
) -> None:
    updates: dict[str, Any] = {
        "command": item.command,
        "status": item.status,
        "exit_code": item.exit_code,
    }
    output = item.aggregated_output
    if output not in (None, ""):
        updates["output"] = _truncate_span_value(output, span_data_max_chars)
    _apply_span_updates(
        span,
        updates,
        span_data_max_chars,
    )


def _update_mcp_tool_span(
    span: Any, item: McpToolCallItem, span_data_max_chars: int | None
) -> None:
    _apply_span_updates(
        span,
        {
            "server": item.server,
            "tool": item.tool,
            "status": item.status,
            "arguments": _truncate_span_value(_maybe_as_dict(item.arguments), span_data_max_chars),
            "result": _truncate_span_value(_maybe_as_dict(item.result), span_data_max_chars),
            "error": _truncate_span_value(_maybe_as_dict(item.error), span_data_max_chars),
        },
        span_data_max_chars,
    )


def _update_reasoning_span(span: Any, item: ReasoningItem, span_data_max_chars: int | None) -> None:
    _apply_span_updates(
        span,
        {"text": _truncate_span_value(item.text, span_data_max_chars)},
        span_data_max_chars,
    )


def _build_default_response(args: CodexToolCallArguments) -> str:
    input_summary = "with inputs." if args.get("inputs") else "with no inputs."
    return f"Codex task completed {input_summary}"


def _is_command_execution_item(item: ThreadItem) -> TypeGuard[CommandExecutionItem]:
    return isinstance(item, CommandExecutionItem)


def _is_mcp_tool_call_item(item: ThreadItem) -> TypeGuard[McpToolCallItem]:
    return isinstance(item, McpToolCallItem)


def _is_reasoning_item(item: ThreadItem) -> TypeGuard[ReasoningItem]:
    return isinstance(item, ReasoningItem)
