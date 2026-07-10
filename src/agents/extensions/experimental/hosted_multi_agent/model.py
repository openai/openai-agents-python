from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args, overload

from openai import AsyncOpenAI
from openai.resources.beta.responses.responses import AsyncResponsesConnection
from openai.types import ChatModel
from openai.types.beta.beta_responses_client_event_param import BetaResponsesClientEventParam
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseOutputItem,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseStreamEvent,
    ResponseUsage,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import BaseModel, TypeAdapter, ValidationError

from ....agent_output import AgentOutputSchemaBase
from ....exceptions import UserError
from ....handoffs import Handoff
from ....items import TResponseInputItem
from ....model_settings import ModelSettings
from ....models._response_terminal import (
    response_error_event_failure_error,
    response_terminal_failure_error,
)
from ....models._run_context import get_model_run_owner
from ....models.openai_responses import OpenAIResponsesModel, _is_openai_omitted_value
from ....tool import Tool
from ....tool_context import ToolContext

_BETA_ID = "responses_multi_agent=v1"
_ROOT_AGENT_NAME = "/root"
_HOSTED_PROVIDER_ITEM_TYPES = frozenset(
    {"agent_message", "multi_agent_call", "multi_agent_call_output"}
)
_FUNCTION_CALL_TYPE = "function_call"
_FUNCTION_OUTPUT_TYPE = "function_call_output"
_RESPONSE_OUTPUT_ADAPTER: TypeAdapter[ResponseOutputItem] = TypeAdapter(ResponseOutputItem)
_RESPONSE_USAGE_ADAPTER: TypeAdapter[ResponseUsage] = TypeAdapter(ResponseUsage)


def _stable_response_output_types() -> frozenset[str]:
    annotated_args = get_args(ResponseOutputItem)
    output_union = annotated_args[0] if annotated_args else ResponseOutputItem
    item_types: set[str] = set()
    for output_class in get_args(output_union):
        type_field = getattr(output_class, "model_fields", {}).get("type")
        annotation = getattr(type_field, "annotation", None)
        item_types.update(value for value in get_args(annotation) if isinstance(value, str))
    return frozenset(item_types)


_STABLE_RESPONSE_OUTPUT_TYPES = _stable_response_output_types()


async def _send_websocket_event(
    connection: AsyncResponsesConnection,
    event: dict[str, Any],
) -> None:
    await connection.send(cast(BetaResponsesClientEventParam, event))


@dataclass(frozen=True)
class HostedMultiAgentConfig:
    """Configuration for the Responses API hosted multi-agent beta."""

    max_concurrent_subagents: int | None = None
    """Maximum active subagents across the hosted tree, excluding the root agent."""

    def __post_init__(self) -> None:
        value = self.max_concurrent_subagents
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise ValueError("max_concurrent_subagents must be a positive integer or None.")


def _normalize_hosted_multi_agent_config(
    config: HostedMultiAgentConfig | Mapping[str, Any] | None,
) -> HostedMultiAgentConfig:
    if config is None:
        return HostedMultiAgentConfig()
    if isinstance(config, HostedMultiAgentConfig):
        return config
    return HostedMultiAgentConfig(**config)


@dataclass(frozen=True)
class HostedAgentMetadata:
    """Hosted-agent attribution attached to a beta response item."""

    agent_name: str
    phase: str | None = None


@dataclass
class _PendingInjection:
    call_id: str
    input_item: dict[str, Any]


@dataclass
class _ActiveWebSocketResponse:
    connection: AsyncResponsesConnection
    loop: asyncio.AbstractEventLoop
    owner: object
    response_id: str | None = None
    response_template: object | None = None
    pending_call_ids: set[str] = field(default_factory=set)
    sent_call_ids: set[str] = field(default_factory=set)
    pending_injections: deque[_PendingInjection] = field(default_factory=deque)
    delivered_item_keys: set[tuple[str, str]] = field(default_factory=set)
    completed_response: object | None = None
    fallback_input: list[dict[str, Any]] = field(default_factory=list)
    accumulated_usage: ResponseUsage | None = None
    request_usages: list[ResponseUsage] = field(default_factory=list)
    request_count: int = 1
    last_sequence_number: int = 0


def _get_field(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def get_hosted_agent_metadata(value: object) -> HostedAgentMetadata | None:
    """Return hosted-agent attribution from an item or function-tool context."""

    if isinstance(value, ToolContext):
        value = value.tool_call
    else:
        tool_call = _get_field(value, "tool_call")
        if tool_call is not None:
            value = tool_call

    if value is None:
        return None

    agent = _get_field(value, "agent")
    agent_name = _get_field(agent, "agent_name") if agent is not None else None
    if not isinstance(agent_name, str) or not agent_name:
        return None

    phase = _get_field(value, "phase")
    return HostedAgentMetadata(
        agent_name=agent_name,
        phase=phase if isinstance(phase, str) else None,
    )


def _model_dump(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_unset=True, warnings=False)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return cast(dict[str, Any], model_dump(mode="python", exclude_unset=True))
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return dict(data)
    raise UserError(f"Unsupported hosted multi-agent response value: {type(value).__name__}")


def _is_root_final_message(payload: Mapping[str, Any]) -> bool:
    agent = payload.get("agent")
    agent_name = _get_field(agent, "agent_name") if agent is not None else None
    return (
        payload.get("type") == "message"
        and agent_name == _ROOT_AGENT_NAME
        and payload.get("phase") == "final_answer"
    )


def _output_item_key(value: object) -> tuple[str, str] | None:
    payload = _model_dump(value)
    item_type = payload.get("type")
    if not isinstance(item_type, str):
        return None
    for field_name in ("id", "call_id"):
        identifier = payload.get(field_name)
        if isinstance(identifier, str) and identifier:
            return item_type, identifier
    return None


def _normalize_output_item(value: object) -> ResponseOutputItem | None:
    payload = _model_dump(value)
    item_type = payload.get("type")

    if item_type in _HOSTED_PROVIDER_ITEM_TYPES:
        return None
    if item_type == "message" and not _is_root_final_message(payload):
        return None
    if not isinstance(item_type, str) or item_type not in _STABLE_RESPONSE_OUTPUT_TYPES:
        return None

    try:
        return _RESPONSE_OUTPUT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise UserError(
            f"Hosted multi-agent returned an invalid stable output item of type '{item_type}'."
        ) from exc


def _normalize_output_items(values: list[object]) -> list[ResponseOutputItem]:
    output: list[ResponseOutputItem] = []
    for value in values:
        item = _normalize_output_item(value)
        if item is not None:
            output.append(item)
    return output


def _normalize_response_usage(value: object) -> ResponseUsage:
    normalized = _RESPONSE_USAGE_ADAPTER.validate_python(value, from_attributes=True)
    input_details = _get_field(value, "input_tokens_details")
    cache_write_tokens = _get_field(input_details, "cache_write_tokens")
    if not isinstance(cache_write_tokens, int):
        return normalized

    normalized_input_details = _model_dump(normalized.input_tokens_details)
    normalized_input_details["cache_write_tokens"] = cache_write_tokens
    return normalized.model_copy(
        update={
            "input_tokens_details": type(normalized.input_tokens_details).model_validate(
                normalized_input_details
            )
        }
    )


def _merge_response_usage(
    previous: ResponseUsage | None,
    current: ResponseUsage,
) -> ResponseUsage:
    if previous is None:
        return current

    payload = current.model_dump(mode="python", exclude_unset=False, warnings=False)
    payload["input_tokens"] = previous.input_tokens + current.input_tokens
    payload["output_tokens"] = previous.output_tokens + current.output_tokens
    payload["total_tokens"] = previous.total_tokens + current.total_tokens

    previous_input_details = _model_dump(previous.input_tokens_details)
    current_input_details = _model_dump(current.input_tokens_details)
    merged_input_details = {
        **previous_input_details,
        **current_input_details,
        "cached_tokens": (previous_input_details.get("cached_tokens") or 0)
        + (current_input_details.get("cached_tokens") or 0),
        "cache_write_tokens": (previous_input_details.get("cache_write_tokens") or 0)
        + (current_input_details.get("cache_write_tokens") or 0),
    }
    payload["input_tokens_details"] = merged_input_details

    previous_output_details = _model_dump(previous.output_tokens_details)
    current_output_details = _model_dump(current.output_tokens_details)
    payload["output_tokens_details"] = {
        **previous_output_details,
        **current_output_details,
        "reasoning_tokens": (previous_output_details.get("reasoning_tokens") or 0)
        + (current_output_details.get("reasoning_tokens") or 0),
    }
    merged = _RESPONSE_USAGE_ADAPTER.validate_python(payload)
    return merged.model_copy(
        update={
            "input_tokens_details": type(current.input_tokens_details).model_validate(
                merged_input_details
            )
        }
    )


def _normalize_response(
    value: object,
    *,
    exclude_item_keys: set[tuple[str, str]] | None = None,
    fallback_output: list[object] | None = None,
    accumulated_usage: ResponseUsage | None = None,
    request_usages: list[ResponseUsage] | None = None,
    request_count: int = 1,
) -> Response:
    payload = _model_dump(value)
    output = _get_field(value, "output")
    if not isinstance(output, list):
        raise UserError("Hosted multi-agent response did not contain an output list.")

    if not output and fallback_output:
        output = fallback_output
    if exclude_item_keys:
        output = [item for item in output if _output_item_key(item) not in exclude_item_keys]

    # Preserve typed nested response fields such as usage while replacing only the output union.
    normalized_usage = accumulated_usage
    current_usage: ResponseUsage | None = None
    for field_name in Response.model_fields:
        field_value = _get_field(value, field_name)
        if field_value is not None:
            if field_name == "usage":
                current_usage = _normalize_response_usage(field_value)
                normalized_usage = _merge_response_usage(
                    normalized_usage,
                    current_usage,
                )
            else:
                payload[field_name] = field_value
    if normalized_usage is not None and request_count > 1:
        individual_usages = list(request_usages or [])
        if current_usage is not None:
            individual_usages.append(current_usage)
        object.__setattr__(
            normalized_usage,
            "_agents_sdk_request_usages",
            individual_usages,
        )
        object.__setattr__(normalized_usage, "_agents_sdk_request_count", request_count)
    payload["usage"] = normalized_usage
    payload["output"] = _normalize_output_items(output)
    return Response.model_construct(**payload)


def _logical_pause_response(
    active: _ActiveWebSocketResponse,
    output: list[object],
) -> Response:
    template = active.response_template
    if template is None or active.response_id is None:
        raise UserError("Hosted multi-agent received a function call before response.created.")

    payload = _model_dump(template)
    for field_name in Response.model_fields:
        field_value = _get_field(template, field_name)
        if field_value is not None:
            payload[field_name] = field_value
    payload["id"] = active.response_id
    payload["status"] = "completed"
    payload["usage"] = None
    payload["output"] = _normalize_output_items(output)
    return Response.model_construct(**payload)


def _construct_event(event_type: str, payload: dict[str, Any]) -> ResponseStreamEvent | None:
    event_classes: dict[str, type[BaseModel]] = {
        "response.output_item.added": ResponseOutputItemAddedEvent,
        "response.output_item.done": ResponseOutputItemDoneEvent,
        "response.completed": ResponseCompletedEvent,
        "response.failed": ResponseFailedEvent,
        "response.incomplete": ResponseIncompleteEvent,
    }
    event_class = event_classes.get(event_type)
    if event_class is None:
        return None
    return cast(ResponseStreamEvent, event_class.model_construct(**payload))


class OpenAIHostedMultiAgentModel(OpenAIResponsesModel):
    """Experimental Responses model backed by OpenAI-hosted multi-agent orchestration."""

    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI | None = None,
        *,
        config: HostedMultiAgentConfig | Mapping[str, Any] | None = None,
        model_is_explicit: bool = True,
    ) -> None:
        super().__init__(
            model=model,
            openai_client=cast(AsyncOpenAI, openai_client),
            model_is_explicit=model_is_explicit,
        )
        self.config = _normalize_hosted_multi_agent_config(config)
        self._active_response: _ActiveWebSocketResponse | None = None
        self._request_lock: asyncio.Lock | None = None
        self._request_lock_loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop] | None = None

    def _validate_beta_settings(
        self,
        model_settings: ModelSettings,
        tools: list[Tool],
        handoffs: list[Handoff],
    ) -> None:
        if handoffs:
            raise UserError(
                "OpenAI hosted multi-agent cannot be combined with SDK handoffs. "
                "Use local function tools or agents-as-tools instead."
            )

        approval_tool_names = sorted(
            tool.name for tool in tools if getattr(tool, "needs_approval", False) is not False
        )
        if approval_tool_names:
            tool_names = ", ".join(approval_tool_names)
            raise UserError(
                "OpenAI hosted multi-agent does not support SDK tool approval interruptions "
                "because an active hosted response cannot be restored from serialized RunState. "
                f"Remove needs_approval from these tools: {tool_names}."
            )

        extra_args = model_settings.extra_args or {}
        extra_body = (
            model_settings.extra_body if isinstance(model_settings.extra_body, Mapping) else {}
        )
        for reserved_key in ("multi_agent", "betas"):
            if reserved_key in extra_args or reserved_key in extra_body:
                raise UserError(
                    f"Configure '{reserved_key}' through OpenAIHostedMultiAgentModel, "
                    "not ModelSettings."
                )

        if "max_tool_calls" in extra_args or "max_tool_calls" in extra_body:
            raise UserError("max_tool_calls is not supported by the hosted multi-agent beta.")

        if model_settings.reasoning is not None:
            reasoning = _model_dump(model_settings.reasoning)
            if reasoning.get("summary") is not None:
                raise UserError(
                    "reasoning.summary is not supported by the hosted multi-agent beta."
                )

    def _build_response_create_kwargs(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        stream: bool = False,
        prompt: ResponsePromptParam | None = None,
    ) -> dict[str, Any]:
        self._validate_beta_settings(model_settings, tools, handoffs)
        kwargs = super()._build_response_create_kwargs(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            stream=stream,
            prompt=prompt,
        )
        multi_agent: dict[str, Any] = {"enabled": True}
        if self.config.max_concurrent_subagents is not None:
            multi_agent["max_concurrent_subagents"] = self.config.max_concurrent_subagents
        kwargs["multi_agent"] = multi_agent
        kwargs["betas"] = [_BETA_ID]
        return kwargs

    def _get_request_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if (
            self._request_lock is None
            or self._request_lock_loop_ref is None
            or self._request_lock_loop_ref() is not loop
        ):
            self._request_lock = asyncio.Lock()
            self._request_lock_loop_ref = weakref.ref(loop)
        return self._request_lock

    def _prepare_websocket_request(
        self,
        create_kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
        kwargs = dict(create_kwargs)
        extra_headers = kwargs.pop("extra_headers", None)
        extra_query = kwargs.pop("extra_query", None)
        extra_body = kwargs.pop("extra_body", None)
        kwargs.pop("timeout", None)
        kwargs.pop("stream", None)
        kwargs.pop("betas", None)

        headers: dict[str, str] = {}
        if extra_headers is not None and not _is_openai_omitted_value(extra_headers):
            if not isinstance(extra_headers, Mapping):
                raise UserError("Hosted multi-agent WebSocket headers must be a mapping.")
            headers.update(
                {
                    str(key): str(value)
                    for key, value in extra_headers.items()
                    if not _is_openai_omitted_value(value)
                }
            )
        for existing_key in list(headers):
            if existing_key.lower() == "openai-beta":
                del headers[existing_key]
        headers["OpenAI-Beta"] = _BETA_ID

        query: dict[str, Any] = {}
        if extra_query is not None and not _is_openai_omitted_value(extra_query):
            if not isinstance(extra_query, Mapping):
                raise UserError("Hosted multi-agent WebSocket query must be a mapping.")
            query.update(extra_query)

        frame: dict[str, Any] = {"type": "response.create"}
        for key, value in kwargs.items():
            if not _is_openai_omitted_value(value):
                frame[key] = value
        if extra_body is not None and not _is_openai_omitted_value(extra_body):
            if not isinstance(extra_body, Mapping):
                raise UserError("Hosted multi-agent WebSocket extra_body must be a mapping.")
            frame.update(
                {
                    str(key): value
                    for key, value in extra_body.items()
                    if not _is_openai_omitted_value(value)
                }
            )
        frame["type"] = "response.create"
        return frame, headers, query

    async def _start_active_response(
        self,
        create_kwargs: dict[str, Any],
        owner: object,
    ) -> _ActiveWebSocketResponse:
        frame, headers, query = self._prepare_websocket_request(create_kwargs)
        manager = self._get_client().beta.responses.connect(
            extra_headers=headers,
            extra_query=query,
            max_retries=0,
        )
        connection = await manager.enter()

        active = _ActiveWebSocketResponse(
            connection=connection,
            loop=asyncio.get_running_loop(),
            owner=owner,
        )
        try:
            await _send_websocket_event(connection, frame)
        except BaseException:
            with contextlib.suppress(Exception):
                await connection.close()
            raise
        self._active_response = active
        return active

    async def _close_active_response(
        self,
        active: _ActiveWebSocketResponse | None = None,
    ) -> None:
        target = active or self._active_response
        if target is None:
            return
        if self._active_response is target:
            self._active_response = None
        if target.loop is not asyncio.get_running_loop():
            connection = getattr(target.connection, "_connection", target.connection)
            transport = getattr(connection, "transport", None)
            abort = getattr(transport, "abort", None)
            if callable(abort):
                abort()
            return
        await target.connection.close()

    async def close(self) -> None:
        await self._close_active_response()
        self._request_lock = None
        self._request_lock_loop_ref = None

    async def _cleanup_on_run_end(self, owner: object) -> None:
        active = self._active_response
        if active is not None and active.owner is owner:
            await self._close_active_response(active)

    @staticmethod
    def _matching_function_outputs(
        create_kwargs: dict[str, Any],
        active: _ActiveWebSocketResponse,
    ) -> list[dict[str, Any]]:
        request_input = create_kwargs.get("input")
        if not isinstance(request_input, list):
            return []

        outputs: list[dict[str, Any]] = []
        for item in request_input:
            try:
                payload = _model_dump(item)
            except UserError:
                continue
            call_id = payload.get("call_id")
            if (
                payload.get("type") == _FUNCTION_OUTPUT_TYPE
                and isinstance(call_id, str)
                and call_id in active.pending_call_ids
                and call_id not in active.sent_call_ids
            ):
                outputs.append(payload)
        return outputs

    async def _inject_function_outputs(
        self,
        active: _ActiveWebSocketResponse,
        create_kwargs: dict[str, Any],
    ) -> None:
        unsent_call_ids = active.pending_call_ids - active.sent_call_ids
        if not unsent_call_ids:
            return

        outputs = self._matching_function_outputs(create_kwargs, active)
        output_call_ids = {
            cast(str, item["call_id"]) for item in outputs if isinstance(item.get("call_id"), str)
        }
        missing_call_ids = unsent_call_ids - output_call_ids
        if missing_call_ids:
            missing = ", ".join(sorted(missing_call_ids))
            raise UserError(
                "OpenAIHostedMultiAgentModel has an active response waiting for function "
                f"outputs, but the next model input did not contain outputs for: {missing}."
            )

        for output in outputs:
            call_id = cast(str, output["call_id"])
            await _send_websocket_event(
                active.connection,
                {
                    "type": "response.inject",
                    "response_id": active.response_id,
                    "input": [output],
                },
            )
            active.sent_call_ids.add(call_id)
            active.pending_injections.append(_PendingInjection(call_id=call_id, input_item=output))

    @staticmethod
    def _record_created_event(active: _ActiveWebSocketResponse, event: object) -> None:
        response = _get_field(event, "response")
        response_id = _get_field(response, "id") if response is not None else None
        if not isinstance(response_id, str) or not response_id:
            raise UserError("Hosted multi-agent response.created did not contain a response ID.")
        active.response_id = response_id
        active.response_template = response

    @staticmethod
    def _record_injection_ack(active: _ActiveWebSocketResponse) -> None:
        if not active.pending_injections:
            raise UserError(
                "Hosted multi-agent received response.inject.created without a pending injection."
            )
        pending = active.pending_injections.popleft()
        active.pending_call_ids.discard(pending.call_id)
        active.sent_call_ids.discard(pending.call_id)

    @staticmethod
    def _record_injection_failure(
        active: _ActiveWebSocketResponse,
        event: object,
    ) -> None:
        if not active.pending_injections:
            raise UserError(
                "Hosted multi-agent received response.inject.failed without a pending injection."
            )
        pending = active.pending_injections.popleft()
        active.pending_call_ids.discard(pending.call_id)
        active.sent_call_ids.discard(pending.call_id)

        error = _get_field(event, "error")
        code = _get_field(error, "code") if error is not None else None
        if code != "response_already_completed":
            raise UserError(
                "Hosted multi-agent function output injection failed"
                + (f" with code '{code}'." if isinstance(code, str) else ".")
            )

        failed_input = _get_field(event, "input")
        if not isinstance(failed_input, list):
            failed_input = [pending.input_item]
        for item in failed_input:
            active.fallback_input.append(_model_dump(item))

    async def _restart_after_completed_injection(
        self,
        active: _ActiveWebSocketResponse,
        create_kwargs: dict[str, Any],
    ) -> _ActiveWebSocketResponse:
        completed_event = active.completed_response
        response = _get_field(completed_event, "response") if completed_event is not None else None
        response_id = _get_field(response, "id") if response is not None else None
        if not isinstance(response_id, str) or not response_id:
            raise UserError(
                "Hosted multi-agent could not continue after a completed response injection."
            )
        completed_usage = _get_field(response, "usage")
        if completed_usage is not None:
            normalized_completed_usage = _normalize_response_usage(completed_usage)
            active.request_usages.append(normalized_completed_usage)
            active.accumulated_usage = _merge_response_usage(
                active.accumulated_usage,
                normalized_completed_usage,
            )
        fallback_input = list(active.fallback_input)

        continuation_kwargs = dict(create_kwargs)
        continuation_kwargs["input"] = fallback_input
        conversation = continuation_kwargs.get("conversation")
        if conversation is not None and not _is_openai_omitted_value(conversation):
            continuation_kwargs.pop("previous_response_id", None)
        else:
            continuation_kwargs["previous_response_id"] = response_id

        frame, _, _ = self._prepare_websocket_request(continuation_kwargs)
        active.response_id = None
        active.response_template = None
        active.pending_call_ids.clear()
        active.sent_call_ids.clear()
        active.pending_injections.clear()
        active.delivered_item_keys.clear()
        active.completed_response = None
        active.fallback_input.clear()
        active.request_count += 1
        active.last_sequence_number = 0
        await _send_websocket_event(active.connection, frame)
        return active

    async def _iter_websocket_turn(
        self,
        create_kwargs: dict[str, Any],
    ) -> AsyncIterator[ResponseStreamEvent]:
        reached_boundary = False
        owner = get_model_run_owner()
        if owner is None:
            owner = asyncio.current_task()
        if owner is None:
            raise UserError("Hosted multi-agent could not identify the current model run.")
        async with self._get_request_lock():
            active = self._active_response
            owns_active = False
            try:
                if active is None:
                    active = await self._start_active_response(create_kwargs, owner)
                    owns_active = True
                else:
                    if active.owner is not owner:
                        raise UserError(
                            "OpenAIHostedMultiAgentModel already has a paused response owned by "
                            "another agent run. Use a separate model instance for concurrent runs."
                        )
                    owns_active = True
                    if active.loop is not asyncio.get_running_loop():
                        raise UserError(
                            "An active hosted multi-agent WebSocket response cannot be resumed "
                            "from a different event loop."
                        )
                    await self._inject_function_outputs(active, create_kwargs)

                current_output: list[object] = []
                while True:
                    if active.completed_response is not None and not active.pending_injections:
                        if active.fallback_input:
                            active = await self._restart_after_completed_injection(
                                active, create_kwargs
                            )
                            current_output = []
                            continue

                        completed_event = active.completed_response
                        response = _get_field(completed_event, "response")
                        if response is None:
                            raise UserError(
                                "Hosted multi-agent response.completed did not contain a response."
                            )
                        normalized_response = _normalize_response(
                            response,
                            exclude_item_keys=active.delivered_item_keys,
                            fallback_output=current_output,
                            accumulated_usage=active.accumulated_usage,
                            request_usages=active.request_usages,
                            request_count=active.request_count,
                        )
                        payload = _model_dump(completed_event)
                        payload["response"] = normalized_response
                        normalized_event = _construct_event("response.completed", payload)
                        if normalized_event is None:
                            raise UserError(
                                "Hosted multi-agent could not normalize response.completed."
                            )
                        await self._close_active_response(active)
                        reached_boundary = True
                        yield normalized_event
                        return

                    event = await active.connection.recv()
                    event_type = _get_field(event, "type")
                    sequence_number = _get_field(event, "sequence_number")
                    if isinstance(sequence_number, int):
                        active.last_sequence_number = sequence_number

                    if event_type == "response.created":
                        self._record_created_event(active, event)
                    elif event_type == "response.inject.created":
                        self._record_injection_ack(active)
                    elif event_type == "response.inject.failed":
                        self._record_injection_failure(active, event)
                    elif event_type == "response.completed":
                        active.completed_response = event
                        continue
                    elif event_type in {
                        "response.failed",
                        "response.incomplete",
                        "error",
                        "response.error",
                    }:
                        payload = _model_dump(event)
                        response = _get_field(event, "response")
                        if response is not None:
                            payload["response"] = _normalize_response(
                                response,
                                accumulated_usage=active.accumulated_usage,
                                request_usages=active.request_usages,
                                request_count=active.request_count,
                            )
                        normalized = _construct_event(cast(str, event_type), payload)
                        await self._close_active_response(active)
                        reached_boundary = True
                        yield (
                            normalized
                            if normalized is not None
                            else cast(ResponseStreamEvent, event)
                        )
                        return

                    payload = _model_dump(event)
                    normalized_item: ResponseOutputItem | None = None
                    if event_type in {"response.output_item.added", "response.output_item.done"}:
                        item = _get_field(event, "item")
                        if item is not None:
                            normalized_item = _normalize_output_item(item)
                            if normalized_item is not None:
                                payload["item"] = normalized_item
                    should_normalize = normalized_item is not None or event_type not in {
                        "response.output_item.added",
                        "response.output_item.done",
                    }
                    normalized = (
                        _construct_event(event_type, payload)
                        if isinstance(event_type, str) and should_normalize
                        else None
                    )
                    yield normalized if normalized is not None else cast(ResponseStreamEvent, event)

                    if event_type != "response.output_item.done":
                        continue
                    item = _get_field(event, "item")
                    if item is None:
                        continue
                    current_output.append(item)
                    if _get_field(item, "type") != _FUNCTION_CALL_TYPE:
                        continue
                    call_id = _get_field(item, "call_id")
                    if not isinstance(call_id, str) or not call_id:
                        raise UserError(
                            "Hosted multi-agent function call did not contain a call ID."
                        )
                    active.pending_call_ids.add(call_id)
                    for output_item in current_output:
                        key = _output_item_key(output_item)
                        if key is not None:
                            active.delivered_item_keys.add(key)
                    logical_response = _logical_pause_response(active, current_output)
                    reached_boundary = True
                    yield ResponseCompletedEvent.model_construct(
                        type="response.completed",
                        sequence_number=active.last_sequence_number,
                        response=logical_response,
                        hosted_multi_agent_pause=True,
                    )
                    return
            except BaseException:
                if owns_active and not reached_boundary:
                    with contextlib.suppress(Exception):
                        await self._close_active_response(active)
                raise

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[True],
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[False],
        prompt: ResponsePromptParam | None = None,
    ) -> Response: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        stream: Literal[True] | Literal[False] = False,
        prompt: ResponsePromptParam | None = None,
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        kwargs = self._build_response_create_kwargs(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            stream=True,
            prompt=prompt,
        )
        if stream:
            return self._iter_websocket_turn(kwargs)

        final_response: Response | None = None
        async for event in self._iter_websocket_turn(kwargs):
            event_type = _get_field(event, "type")
            if isinstance(event, ResponseCompletedEvent):
                final_response = event.response
            elif event_type in {"response.failed", "response.incomplete"}:
                response = _get_field(event, "response")
                raise response_terminal_failure_error(
                    cast(str, event_type),
                    response if isinstance(response, Response) else None,
                )
            elif event_type in {"error", "response.error"}:
                raise response_error_event_failure_error(cast(str, event_type), event)

        if final_response is None:
            raise UserError(
                "Hosted multi-agent WebSocket turn ended without a logical or terminal response."
            )
        return final_response
