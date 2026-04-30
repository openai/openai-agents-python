from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeGuard, cast, get_args, overload

import httpx
from openai import AsyncOpenAI, NotGiven, Omit, omit
from openai.types import ChatModel
from openai.types.responses import (
    ApplyPatchToolParam,
    CustomToolParam,
    FileSearchToolParam,
    FunctionToolParam,
    Response,
    ResponseCompletedEvent,
    ResponseIncludable,
    ResponseStreamEvent,
    ResponseTextConfigParam,
    ToolParam as ResponsesToolParam,
    ToolSearchToolParam,
    response_create_params,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.tool_param import LocalShell

from .. import _debug
from .._tool_identity import (
    get_explicit_function_tool_namespace,
    get_function_tool_namespace_description,
)
from ..agent_output import AgentOutputSchemaBase
from ..computer import AsyncComputer, Computer
from ..exceptions import UserError
from ..handoffs import Handoff
from ..items import ItemHelpers, ModelResponse, TResponseInputItem
from ..logger import logger
from ..model_settings import MCPToolChoice
from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest
from ..tool import (
    ApplyPatchTool,
    CodeInterpreterTool,
    ComputerTool,
    CustomTool,
    FileSearchTool,
    FunctionTool,
    HostedMCPTool,
    ImageGenerationTool,
    LocalShellTool,
    ShellTool,
    ShellToolEnvironment,
    Tool,
    ToolSearchTool,
    WebSearchTool,
    has_required_tool_search_surface,
    validate_responses_tool_search_configuration,
)
from ..tracing import SpanError, response_span
from ..usage import Usage, model_usage_to_span_usage
from ..util._json import _to_dump_compatible
from ..version import __version__
from ._openai_retry import get_openai_retry_advice
from ._retry_runtime import (
    should_disable_provider_managed_retries,
    should_disable_websocket_pre_event_retries,
)
from .fake_id import FAKE_RESPONSES_ID
from .interface import Model, ModelTracing
from .openai_client_utils import is_official_openai_base_url, is_official_openai_client

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


_USER_AGENT = f"Agents/Python {__version__}"
_HEADERS = {"User-Agent": _USER_AGENT}

# Override headers used by the Responses API.
_HEADERS_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar(
    "openai_responses_headers_override", default=None
)
_RESPONSE_INCLUDABLE_VALUES = frozenset(
    value for value in get_args(ResponseIncludable) if isinstance(value, str)
)


class _NamespaceToolParam(TypedDict):
    type: Literal["namespace"]
    name: str
    description: str
    tools: list[FunctionToolParam]


def _json_dumps_default(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json", exclude_none=True)
        except TypeError:
            return model_dump()

    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)

    if isinstance(value, Enum):
        return value.value

    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _is_openai_omitted_value(value: Any) -> bool:
    return isinstance(value, Omit | NotGiven)


def _require_responses_tool_param(value: object) -> ResponsesToolParam:
    if not isinstance(value, Mapping):
        raise TypeError(f"Invalid Responses tool param payload: {value!r}")

    tool_type = value.get("type")
    if not isinstance(tool_type, str):
        raise TypeError(f"Invalid Responses tool param payload: {value!r}")

    return cast(ResponsesToolParam, value)


def _is_response_includable(value: object) -> TypeGuard[ResponseIncludable]:
    return isinstance(value, str) and value in _RESPONSE_INCLUDABLE_VALUES


def _coerce_response_includables(values: Sequence[str]) -> list[ResponseIncludable]:
    includables: list[ResponseIncludable] = []
    for value in values:
        if not isinstance(value, str):
            raise UserError(f"Unsupported Responses include value: {value}")
        # ModelSettings.response_include deliberately accepts arbitrary strings so callers can
        # pass through new server-supported flags before the local SDK updates its enum union.
        includables.append(cast(ResponseIncludable, value))
    return includables


def _materialize_responses_tool_params(
    tools: Sequence[ResponsesToolParam],
) -> list[ResponsesToolParam]:
    materialized = _to_dump_compatible(list(tools))
    if not isinstance(materialized, list):
        raise TypeError("Materialized Responses tools payload must be a list.")

    typed_tools: list[ResponsesToolParam] = []
    for tool in materialized:
        typed_tools.append(_require_responses_tool_param(tool))
    return typed_tools


async def _refresh_openai_client_api_key_if_supported(client: Any) -> None:
    """Refresh client auth if the current OpenAI SDK exposes a refresh hook."""
    refresh_api_key = getattr(client, "_refresh_api_key", None)
    if callable(refresh_api_key):
        await refresh_api_key()


def _construct_response_stream_event_from_payload(
    payload: Mapping[str, Any],
) -> ResponseStreamEvent:
    """Parse websocket event payloads via the OpenAI SDK's internal type constructor."""
    try:
        from openai._models import construct_type
    except Exception as exc:  # pragma: no cover - exercised only on SDK incompatibility
        raise RuntimeError(
            "Unable to parse Responses websocket events because the installed OpenAI SDK "
            "does not expose the expected internal type constructor. Please upgrade this SDK "
            "version pair or switch Responses transport back to HTTP."
        ) from exc
    return cast(
        ResponseStreamEvent,
        construct_type(type_=ResponseStreamEvent, value=dict(payload)),
    )


@dataclass(frozen=True)
class _WebsocketRequestTimeouts:
    lock: float | None
    connect: float | None
    send: float | None
    recv: float | None


class _ResponseStreamWithRequestId:
    """Wrap an SDK event stream and retain the originating request ID."""

    _TERMINAL_EVENT_TYPES = {
        "response.completed",
        "response.failed",
        "response.incomplete",
        "response.error",
    }

    def __init__(
        self,
        stream: AsyncIterator[ResponseStreamEvent],
        *,
        request_id: str | None,
        cleanup: Callable[[], Awaitable[object]],
    ) -> None:
        self._stream = stream
        self.request_id = request_id
        self._cleanup = cleanup
        self._closed = False
        self._stream_close_complete = False
        self._cleanup_complete = False
        self._yielded_terminal_event = False

    def __aiter__(self) -> _ResponseStreamWithRequestId:
        return self

    async def __anext__(self) -> ResponseStreamEvent:
        if self._closed:
            raise StopAsyncIteration

        try:
            event = await self._stream.__anext__()
        except StopAsyncIteration:
            self._closed = True
            await self._cleanup_after_exhaustion()
            raise

        self._attach_request_id(event)
        event_type = getattr(event, "type", None)
        if event_type in self._TERMINAL_EVENT_TYPES:
            self._yielded_terminal_event = True
        return event

    async def aclose(self) -> None:
        self._closed = True
        try:
            await self._close_stream_once()
        finally:
            await self._cleanup_once()

    async def close(self) -> None:
        await self.aclose()

    def _attach_request_id(self, event: ResponseStreamEvent) -> None:
        if self.request_id is None:
            return

        response = getattr(event, "response", None)
        if response is None:
            return

        try:
            response._request_id = self.request_id
        except Exception:
            return

    async def _cleanup_once(self) -> None:
        if self._cleanup_complete:
            return
        self._cleanup_complete = True
        await self._cleanup()

    async def _cleanup_after_exhaustion(self) -> None:
        try:
            await self._cleanup_once()
        except Exception as exc:
            if self._yielded_terminal_event:
                logger.debug(f"Ignoring stream cleanup error after terminal event: {exc}")
                return
            raise

    async def _close_stream_once(self) -> None:
        if self._stream_close_complete:
            return
        self._stream_close_complete = True

        aclose = getattr(self._stream, "aclose", None)
        if callable(aclose):
            await aclose()
            return

        close = getattr(self._stream, "close", None)
        if callable(close):
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result


class ResponsesWebSocketError(RuntimeError):
    """Error raised for websocket transport error frames."""

    def __init__(self, payload: Mapping[str, Any]):
        event_type = str(payload.get("type") or "error")
        self.event_type = event_type
        self.payload = dict(payload)

        error_data = payload.get("error")
        error_obj = error_data if isinstance(error_data, Mapping) else {}
        self.code = self._coerce_optional_str(error_obj.get("code"))
        self.error_type = self._coerce_optional_str(error_obj.get("type"))
        self.request_id = self._coerce_optional_str(
            payload.get("request_id") or error_obj.get("request_id")
        )
        self.error_message = self._coerce_optional_str(error_obj.get("message"))

        prefix = (
            "Responses websocket error"
            if event_type == "error"
            else f"Responses websocket {event_type}"
        )
        super().__init__(f"{prefix}: {json.dumps(payload, default=_json_dumps_default)}")

    @staticmethod
    def _coerce_optional_str(value: Any) -> str | None:
        return value if isinstance(value, str) else None


def _iter_retry_error_chain(error: Exception):
    current: Exception | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, Exception) else None


def _get_wrapped_websocket_replay_safety(error: Exception) -> str | None:
    replay_safety = getattr(error, "_openai_agents_ws_replay_safety", None)
    return replay_safety if replay_safety in {"safe", "unsafe"} else None


def _did_start_websocket_response(error: Exception) -> bool:
    return bool(getattr(error, "_openai_agents_ws_response_started", False))


def _is_never_sent_websocket_error(error: Exception) -> bool:
    for candidate in _iter_retry_error_chain(error):
        if candidate.__class__.__module__.startswith(
            "websockets"
        ) and candidate.__class__.__name__.startswith("ConnectionClosed"):
            if "client closed" not in str(candidate).lower():
                return True
    return False


def _is_ambiguous_websocket_replay_error(error: Exception) -> bool:
    for candidate in _iter_retry_error_chain(error):
        message = str(candidate)
        if message.startswith(
            "Responses websocket connection closed before a terminal response event."
        ):
            return True
    return False


def _get_websocket_timeout_phase(error: Exception) -> str | None:
    for candidate in _iter_retry_error_chain(error):
        if not isinstance(candidate, TimeoutError):
            continue
        message = str(candidate)
        for phase in ("request lock wait", "connect", "send", "receive"):
            if message.startswith(f"Responses websocket {phase} timed out"):
                return phase
    return None


def _should_retry_pre_event_websocket_disconnect() -> bool:
    return not should_disable_websocket_pre_event_retries()


class OpenAIResponsesModel(Model):
    """
    Implementation of `Model` that uses the OpenAI Responses API.
    """

    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
        *,
        model_is_explicit: bool = True,
    ) -> None:
        self.model = model
        self._model_is_explicit = model_is_explicit
        self._client = openai_client

    def _non_null_or_omit(self, value: Any) -> Any:
        return value if value is not None else omit

    def _supports_default_prompt_cache_key(self) -> bool:
        return is_official_openai_client(self._get_client())

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
        return get_openai_retry_advice(request)

    async def _maybe_aclose_async_iterator(self, iterator: Any) -> None:
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose):
            await aclose()
            return

        close = getattr(iterator, "close", None)
        if callable(close):
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result

    def _schedule_async_iterator_close(self, iterator: Any) -> None:
        task = asyncio.create_task(self._maybe_aclose_async_iterator(iterator))
        task.add_done_callback(self._consume_background_cleanup_task_result)

    @staticmethod
    def _consume_background_cleanup_task_result(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"Background stream cleanup failed after cancellation: {exc}")

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> ModelResponse:
        with response_span(disabled=tracing.is_disabled()) as span_response:
            try:
                response = await self._fetch_response(
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                    stream=False,
                    prompt=prompt,
                )

                if _debug.DONT_LOG_MODEL_DATA:
                    logger.debug("LLM responded")
                else:
                    logger.debug(
                        "LLM resp:\n"
                        f"""{
                            json.dumps(
                                [x.model_dump() for x in response.output],
                                indent=2,
                                ensure_ascii=False,
                            )
                        }\n"""
                    )

                usage = (
                    Usage(
                        requests=1,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        total_tokens=response.usage.total_tokens,
                        input_tokens_details=response.usage.input_tokens_details,
                        output_tokens_details=response.usage.output_tokens_details,
                    )
                    if response.usage
                    else Usage()
                )
                if response.usage:
                    span_response.span_data.usage = model_usage_to_span_usage(usage)

                if tracing.include_data():
                    span_response.span_data.response = response
                    span_response.span_data.input = input
            except Exception as e:
                span_response.set_error(
                    SpanError(
                        message="Error getting response",
                        data={
                            "error": str(e) if tracing.include_data() else e.__class__.__name__,
                        },
                    )
                )
                request_id = getattr(e, "request_id", None)
                logger.error(f"Error getting response: {e}. (request_id: {request_id})")
                raise

        return ModelResponse(
            output=response.output,
            usage=usage,
            response_id=response.id,
            request_id=getattr(response, "_request_id", None),
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        """
        Yields a partial message as it is generated, as well as the usage information.
        """
        with response_span(disabled=tracing.is_disabled()) as span_response:
            try:
                stream = await self._fetch_response(
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                    stream=True,
                    prompt=prompt,
                )

                final_response: Response | None = None
                yielded_terminal_event = False
                close_stream_in_background = False
                try:
                    async for chunk in stream:
                        chunk_type = getattr(chunk, "type", None)
                        if isinstance(chunk, ResponseCompletedEvent):
                            final_response = chunk.response
                        elif chunk_type in {
                            "response.failed",
                            "response.incomplete",
                        }:
                            terminal_response = getattr(chunk, "response", None)
                            if isinstance(terminal_response, Response):
                                final_response = terminal_response
                        if chunk_type in {
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                            "response.error",
                        }:
                            yielded_terminal_event = True
                        yield chunk
                except asyncio.CancelledError:
                    close_stream_in_background = True
                    self._schedule_async_iterator_close(stream)
                    raise
                finally:
                    if not close_stream_in_background:
                        try:
                            await self._maybe_aclose_async_iterator(stream)
                        except Exception as exc:
                            if yielded_terminal_event:
                                logger.debug(
                                    f"Ignoring stream cleanup error after terminal event: {exc}"
                                )
                            else:
                                raise

                if final_response and tracing.include_data():
                    span_response.span_data.response = final_response
                    span_response.span_data.input = input
                if final_response and final_response.usage:
                    span_response.span_data.usage = model_usage_to_span_usage(
                        Usage(
                            requests=1,
                            input_tokens=final_response.usage.input_tokens,
                            output_tokens=final_response.usage.output_tokens,
                            total_tokens=final_response.usage.total_tokens,
                            input_tokens_details=final_response.usage.input_tokens_details,
                            output_tokens_details=final_response.usage.output_tokens_details,
                        )
                    )

            except Exception as e:
                span_response.set_error(
                    SpanError(
                        message="Error streaming response",
                        data={
                            "error": str(e) if tracing.include_data() else e.__class__.__name__,
                        },
                    )
                )
                logger.error(f"Error streaming response: {e}")
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
        create_kwargs = self._build_response_create_kwargs(
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
        client = self._get_client()

        if not stream:
            response = await client.responses.create(**create_kwargs)
            return cast(Response, response)

        streaming_response = getattr(client.responses, "with_streaming_response", None)
        stream_create = getattr(streaming_response, "create", None)
        if not callable(stream_create):
            # Some tests and custom clients only implement `responses.create()`. Fall back to the
            # older path in that case and simply omit request IDs for streamed calls.
            response = await client.responses.create(**create_kwargs)
            return cast(AsyncIterator[ResponseStreamEvent], response)

        # Keep the raw API response open while callers consume the SSE stream so we can expose
        # its request ID on terminal response payloads before cleanup closes the transport.
        api_response_cm = stream_create(**create_kwargs)
        api_response = await api_response_cm.__aenter__()
        try:
            stream_response = await api_response.parse()
        except BaseException as exc:
            await api_response_cm.__aexit__(type(exc), exc, exc.__traceback__)
            raise

        return _ResponseStreamWithRequestId(
            cast(AsyncIterator[ResponseStreamEvent], stream_response),
            request_id=getattr(api_response, "request_id", None),
            cleanup=lambda: api_response_cm.__aexit__(None, None, None),
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
        list_input = ItemHelpers.input_to_new_input_list(input)
        list_input = _to_dump_compatible(list_input)
        list_input = self._remove_openai_responses_api_incompatible_fields(list_input)

        if model_settings.parallel_tool_calls and tools:
            parallel_tool_calls: bool | Omit = True
        elif model_settings.parallel_tool_calls is False:
            parallel_tool_calls = False
        else:
            parallel_tool_calls = omit

        should_omit_model = prompt is not None and not self._model_is_explicit
        effective_request_model: str | ChatModel | None = None if should_omit_model else self.model
        effective_computer_tool_model = Converter.resolve_computer_tool_model(
            request_model=effective_request_model,
            tools=tools,
        )
        tool_choice = Converter.convert_tool_choice(
            model_settings.tool_choice,
            tools=tools,
            handoffs=handoffs,
            model=effective_computer_tool_model,
        )
        if prompt is None:
            converted_tools = Converter.convert_tools(
                tools,
                handoffs,
                model=effective_computer_tool_model,
                tool_choice=model_settings.tool_choice,
            )
        else:
            converted_tools = Converter.convert_tools(
                tools,
                handoffs,
                allow_opaque_tool_search_surface=True,
                model=effective_computer_tool_model,
                tool_choice=model_settings.tool_choice,
            )
        converted_tools_payload = _materialize_responses_tool_params(converted_tools.tools)
        response_format = Converter.get_response_format(output_schema)
        model_param: str | ChatModel | Omit = (
            effective_request_model if effective_request_model is not None else omit
        )
        should_omit_tools = prompt is not None and len(converted_tools_payload) == 0
        # In prompt-managed tool flows without local tools payload, omit only named tool choices
        # that must match an explicit tool list. Keep control literals like "none"/"required".
        should_omit_tool_choice = should_omit_tools and isinstance(tool_choice, dict)
        tools_param: list[ResponsesToolParam] | Omit = (
            converted_tools_payload if not should_omit_tools else omit
        )
        tool_choice_param: response_create_params.ToolChoice | Omit = (
            tool_choice if not should_omit_tool_choice else omit
        )

        include_set: set[ResponseIncludable] = set(converted_tools.includes)
        if model_settings.response_include is not None:
            include_set.update(_coerce_response_includables(model_settings.response_include))
        if model_settings.top_logprobs is not None:
            include_set.add("message.output_text.logprobs")
        include: list[ResponseIncludable] = list(include_set)

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            input_json = json.dumps(
                list_input,
                indent=2,
                ensure_ascii=False,
            )
            tools_json = json.dumps(
                converted_tools_payload,
                indent=2,
                ensure_ascii=False,
            )
            logger.debug(
                f"Calling LLM {self.model} with input:\n"
                f"{input_json}\n"
                f"Tools:\n{tools_json}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice_param}\n"
                f"Response format: {response_format}\n"
                f"Previous response id: {previous_response_id}\n"
                f"Conversation id: {conversation_id}\n"
            )

        extra_args = dict(model_settings.extra_args or {})
        if model_settings.top_logprobs is not None:
            extra_args["top_logprobs"] = model_settings.top_logprobs
        if model_settings.verbosity is not None:
            if response_format is not omit:
                response_format["verbosity"] = model_settings.verbosity  # type: ignore [index]
            else:
                response_format = {"verbosity": model_settings.verbosity}

        stream_param: Literal[True] | Omit = True if stream else omit

        create_kwargs: dict[str, Any] = {
            "previous_response_id": self._non_null_or_omit(previous_response_id),
            "conversation": self._non_null_or_omit(conversation_id),
            "instructions": self._non_null_or_omit(system_instructions),
            "model": model_param,
            "input": list_input,
            "include": include,
            "tools": tools_param,
            "prompt": self._non_null_or_omit(prompt),
            "temperature": self._non_null_or_omit(model_settings.temperature),
            "top_p": self._non_null_or_omit(model_settings.top_p),
            "truncation": self._non_null_or_omit(model_settings.truncation),
            "max_output_tokens": self._non_null_or_omit(model_settings.max_tokens),
            "tool_choice": tool_choice_param,
            "parallel_tool_calls": parallel_tool_calls,
            "stream": cast(Any, stream_param),
            "extra_headers": self._merge_headers(model_settings),
            "extra_query": model_settings.extra_query,
            "extra_body": model_settings.extra_body,
            "text": response_format,
            "store": self._non_null_or_omit(model_settings.store),
            "prompt_cache_retention": self._non_null_or_omit(model_settings.prompt_cache_retention),
            "reasoning": self._non_null_or_omit(model_settings.reasoning),
            "metadata": self._non_null_or_omit(model_settings.metadata),
        }
        duplicate_extra_arg_keys = sorted(set(create_kwargs).intersection(extra_args))
        if duplicate_extra_arg_keys:
            if len(duplicate_extra_arg_keys) == 1:
                key = duplicate_extra_arg_keys[0]
                raise TypeError(
                    f"responses.create() got multiple values for keyword argument '{key}'"
                )
            keys = ", ".join(repr(key) for key in duplicate_extra_arg_keys)
            raise TypeError(f"responses.create() got multiple values for keyword arguments {keys}")
        create_kwargs.update(extra_args)
        return create_kwargs

    def _remove_openai_responses_api_incompatible_fields(self, list_input: list[Any]) -> list[Any]:
        """
        Remove or transform input items that are incompatible with the OpenAI Responses API.

        This data transformation does not always guarantee that items from other provider
        interactions are accepted by the OpenAI Responses API.

        Only items with truthy provider_data are processed.
        This function handles the following incompatibilities:
        - provider_data: Removes fields specific to other providers (e.g., Gemini, Claude).
        - Fake IDs: Removes temporary IDs (FAKE_RESPONSES_ID) that should not be sent to OpenAI.
        - Reasoning items: Filters out provider-specific reasoning items entirely.
        """
        # Early return optimization: if no item has provider_data, return unchanged.
        has_provider_data = any(
            isinstance(item, dict) and item.get("provider_data") for item in list_input
        )
        if not has_provider_data:
            return list_input

        result = []
        for item in list_input:
            cleaned = self._clean_item_for_openai(item)
            if cleaned is not None:
                result.append(cleaned)
        return result

    def _clean_item_for_openai(self, item: Any) -> Any | None:
        # Only process dict items
        if not isinstance(item, dict):
            return item

        # Filter out reasoning items with provider_data (provider-specific reasoning).
        if item.get("type") == "reasoning" and item.get("provider_data"):
            return None

        # Remove fake response ID.
        if item.get("id") == FAKE_RESPONSES_ID:
            del item["id"]

        # Remove provider_data field.
        if "provider_data" in item:
            del item["provider_data"]

        return item

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI()
        if should_disable_provider_managed_retries():
            with_options = getattr(self._client, "with_options", None)
            if callable(with_options):
                return cast(AsyncOpenAI, with_options(max_retries=0))
        return self._client

    def _merge_headers(self, model_settings: ModelSettings):
        return {
            **_HEADERS,
            **(model_settings.extra_headers or {}),
            **(_HEADERS_OVERRIDE.get() or {}),
        }


class OpenAIResponsesWSModel(OpenAIResponsesModel):
    """
    Implementation of `Model` that uses the OpenAI Responses API over a websocket transport.

    The websocket transport currently sends `response.create` frames and always streams events.
    `get_response()` is implemented by consuming the streamed events until a terminal response
    event is received. Successful websocket responses do not currently expose a request ID, so
    `ModelResponse.request_id` remains `None` on this transport.
    """

    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
        *,
        model_is_explicit: bool = True,
    ) -> None:
        super().__init__(
            model=model, openai_client=openai_client, model_is_explicit=model_is_explicit
        )
        self._ws_connection: Any | None = None
        self._ws_connection_identity: tuple[str, tuple[tuple[str, str], ...]] | None = None
        self._ws_connection_loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop] | None = None
        self._ws_request_lock: asyncio.Lock | None = None
        self._ws_request_lock_loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop] | None = (
            None
        )
        self._ws_client_close_generation = 0

    def _supports_default_prompt_cache_key(self) -> bool:
        if self._client.websocket_base_url is not None:
            return is_official_openai_base_url(self._client.websocket_base_url, websocket=True)
        return super()._supports_default_prompt_cache_key()

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
        stateful_request = bool(request.previous_response_id or request.conversation_id)
        wrapped_replay_safety = _get_wrapped_websocket_replay_safety(request.error)
        if wrapped_replay_safety == "unsafe":
            if stateful_request or _did_start_websocket_response(request.error):
                return ModelRetryAdvice(
                    suggested=False,
                    replay_safety="unsafe",
                    reason=str(request.error),
                )
            return ModelRetryAdvice(
                suggested=True,
                reason=str(request.error),
            )
        if wrapped_replay_safety == "safe":
            return ModelRetryAdvice(
                suggested=True,
                replay_safety="safe",
                reason=str(request.error),
            )
        if _is_ambiguous_websocket_replay_error(request.error):
            if stateful_request:
                return ModelRetryAdvice(
                    suggested=False,
                    replay_safety="unsafe",
                    reason=str(request.error),
                )
            return ModelRetryAdvice(
                suggested=True,
                reason=str(request.error),
            )
        timeout_phase = _get_websocket_timeout_phase(request.error)
        if timeout_phase is not None:
            if timeout_phase in {"request lock wait", "connect"}:
                return ModelRetryAdvice(
                    suggested=True,
                    replay_safety="safe",
                    reason=str(request.error),
                )
            if stateful_request:
                return ModelRetryAdvice(
                    suggested=False,
                    replay_safety="unsafe",
                    reason=str(request.error),
                )
            return ModelRetryAdvice(
                suggested=True,
                reason=str(request.error),
            )
        if _is_never_sent_websocket_error(request.error):
            return ModelRetryAdvice(
                suggested=True,
                replay_safety="safe",
                reason=str(request.error),
            )
        return super().get_retry_advice(request)

    def _get_ws_request_lock(self) -> asyncio.Lock:
        running_loop = asyncio.get_running_loop()
        if (
            self._ws_request_lock is None
            or self._ws_request_lock_loop_ref is None
            or self._ws_request_lock_loop_ref() is not running_loop
        ):
            self._ws_request_lock = asyncio.Lock()
            self._ws_request_lock_loop_ref = weakref.ref(running_loop)
        return self._ws_request_lock

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
        create_kwargs = self._build_response_create_kwargs(
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
            return self._iter_websocket_response_events(create_kwargs)

        final_response: Response | None = None
        terminal_event_type: str | None = None
        async for event in self._iter_websocket_response_events(create_kwargs):
            event_type = getattr(event, "type", None)
            if isinstance(event, ResponseCompletedEvent):
                final_response = event.response
                terminal_event_type = event.type
            elif event_type in {"response.incomplete", "response.failed"}:
                terminal_event_type = cast(str, event_type)
                terminal_response = getattr(event, "response", None)
                if isinstance(terminal_response, Response):
                    final_response = terminal_response

        if final_response is None:
            terminal_event_hint = (
                f" Terminal event: `{terminal_event_type}`." if terminal_event_type else ""
            )
            raise RuntimeError(
                "Responses websocket stream ended without a terminal response payload."
                f"{terminal_event_hint}"
            )

        return final_response

    async def _iter_websocket_response_events(
        self, create_kwargs: dict[str, Any]
    ) -> AsyncIterator[ResponseStreamEvent]:
        request_timeout = create_kwargs.get("timeout", omit)
        if _is_openai_omitted_value(request_timeout):
            request_timeout = getattr(self._client, "timeout", None)
        request_timeouts = self._get_websocket_request_timeouts(request_timeout)
        request_close_generation = self._ws_client_close_generation
        request_lock = self._get_ws_request_lock()
        if request_timeouts.lock == 0 and not request_lock.locked():
            # `wait_for(..., timeout=0)` can time out before an uncontended acquire runs.
            await request_lock.acquire()
        else:
            await self._await_websocket_with_timeout(
                request_lock.acquire(),
                request_timeouts.lock,
                "request lock wait",
            )
        try:
            request_frame, ws_url, request_headers = await self._prepare_websocket_request(
                create_kwargs
            )
            retry_pre_event_disconnect = _should_retry_pre_event_websocket_disconnect()
            while True:
                connection = await self._await_websocket_with_timeout(
                    self._ensure_websocket_connection(
                        ws_url, request_headers, connect_timeout=request_timeouts.connect
                    ),
                    request_timeouts.connect,
                    "connect",
                )
                received_any_event = False
                yielded_terminal_event = False
                sent_request_frame = False
                try:
                    # Once we begin awaiting `send()`, treat the request as potentially
                    # transmitted to avoid replaying it on send/close races.
                    sent_request_frame = True
                    await self._await_websocket_with_timeout(
                        connection.send(json.dumps(request_frame, default=_json_dumps_default)),
                        request_timeouts.send,
                        "send",
                    )

                    while True:
                        frame = await self._await_websocket_with_timeout(
                            connection.recv(),
                            request_timeouts.recv,
                            "receive",
                        )
                        if frame is None:
                            raise RuntimeError(
                                "Responses websocket connection closed before a terminal "
                                "response event."
                            )

                        if isinstance(frame, bytes):
                            frame = frame.decode("utf-8")

                        payload = json.loads(frame)
                        event_type = payload.get("type")

                        if event_type == "error":
                            raise ResponsesWebSocketError(payload)
                        if event_type == "response.error":
                            received_any_event = True
                            raise ResponsesWebSocketError(payload)

                        # Successful websocket frames currently expose no per-request ID.
                        # Unlike the HTTP transport, the websocket upgrade response does not
                        # include `x-request-id`, and success events carry no equivalent field.
                        event = _construct_response_stream_event_from_payload(payload)
                        received_any_event = True
                        is_terminal_event = event_type in {
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                            "response.error",
                        }
                        if is_terminal_event:
                            yielded_terminal_event = True
                        yield event

                        if is_terminal_event:
                            return
                except BaseException as exc:
                    is_non_terminal_generator_exit = (
                        isinstance(exc, GeneratorExit) and not yielded_terminal_event
                    )
                    if isinstance(exc, asyncio.CancelledError) or is_non_terminal_generator_exit:
                        self._force_abort_websocket_connection(connection)
                        self._clear_websocket_connection_state()
                    elif not (yielded_terminal_event and isinstance(exc, GeneratorExit)):
                        await self._drop_websocket_connection()

                    if (
                        isinstance(exc, Exception)
                        and received_any_event
                        and not yielded_terminal_event
                    ):
                        setattr(exc, "_openai_agents_ws_replay_safety", "unsafe")  # noqa: B010
                        setattr(exc, "_openai_agents_ws_response_started", True)  # noqa: B010

                    is_pre_event_disconnect = (
                        not received_any_event
                        and isinstance(exc, Exception)
                        and self._should_wrap_pre_event_websocket_disconnect(exc)
                    )
                    # Do not replay a request after the frame was sent; the server may already
                    # be executing it even if no response event arrived yet.
                    is_retryable_pre_event_disconnect = (
                        is_pre_event_disconnect and not sent_request_frame
                    )
                    if (
                        is_pre_event_disconnect
                        and self._ws_client_close_generation != request_close_generation
                    ):
                        raise
                    if retry_pre_event_disconnect and is_retryable_pre_event_disconnect:
                        retry_pre_event_disconnect = False
                        continue
                    if is_pre_event_disconnect:
                        wrapped_disconnect = RuntimeError(
                            "Responses websocket connection closed before any response events "
                            "were received. The feature may not be enabled for this account/model "
                            "yet, or the server closed the connection."
                        )
                        setattr(  # noqa: B010
                            wrapped_disconnect,
                            "_openai_agents_ws_replay_safety",
                            "safe" if is_retryable_pre_event_disconnect else "unsafe",
                        )
                        raise wrapped_disconnect from exc
                    raise
        finally:
            request_lock.release()

    def _should_wrap_pre_event_websocket_disconnect(self, exc: Exception) -> bool:
        if isinstance(exc, UserError):
            return False
        if isinstance(exc, ResponsesWebSocketError):
            return False

        if isinstance(exc, RuntimeError):
            message = str(exc)
            if message.startswith("Responses websocket error:"):
                return False
            return message.startswith(
                "Responses websocket connection closed before a terminal response event."
            )

        exc_module = exc.__class__.__module__
        exc_name = exc.__class__.__name__
        return exc_module.startswith("websockets") and exc_name.startswith("ConnectionClosed")

    def _get_websocket_request_timeouts(self, timeout: Any) -> _WebsocketRequestTimeouts:
        if timeout is None or _is_openai_omitted_value(timeout):
            return _WebsocketRequestTimeouts(lock=None, connect=None, send=None, recv=None)

        if isinstance(timeout, httpx.Timeout):
            return _WebsocketRequestTimeouts(
                lock=None if timeout.pool is None else float(timeout.pool),
                connect=None if timeout.connect is None else float(timeout.connect),
                send=None if timeout.write is None else float(timeout.write),
                recv=None if timeout.read is None else float(timeout.read),
            )

        if isinstance(timeout, int | float):
            timeout_seconds = float(timeout)
            return _WebsocketRequestTimeouts(
                lock=timeout_seconds,
                connect=timeout_seconds,
                send=timeout_seconds,
                recv=timeout_seconds,
            )

        return _WebsocketRequestTimeouts(lock=None, connect=None, send=None, recv=None)

    async def _await_websocket_with_timeout(
        self,
        awaitable: Awaitable[Any],
        timeout_seconds: float | None,
        phase: str,
    ) -> Any:
        if timeout_seconds is None:
            return await awaitable

        if timeout_seconds == 0:
            # `wait_for(..., timeout=0)` can time out before an immediately-ready awaitable runs.
            task = asyncio.ensure_future(awaitable)
            if not task.done():
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise TimeoutError(
                f"Responses websocket {phase} timed out after {timeout_seconds} seconds."
            )

        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Responses websocket {phase} timed out after {timeout_seconds} seconds."
            ) from exc

    async def _prepare_websocket_request(
        self, create_kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], str, dict[str, str]]:
        await _refresh_openai_client_api_key_if_supported(self._client)

        request_kwargs = dict(create_kwargs)
        extra_headers_raw = request_kwargs.pop("extra_headers", None)
        if extra_headers_raw is None or _is_openai_omitted_value(extra_headers_raw):
            extra_headers_raw = {}
        extra_query = request_kwargs.pop("extra_query", None)
        extra_body = request_kwargs.pop("extra_body", None)
        # Request options like `timeout` are transport-level settings, not websocket
        # `response.create` payload fields. They are applied separately when sending/receiving.
        request_kwargs.pop("timeout", None)

        if not isinstance(extra_headers_raw, Mapping):
            raise UserError("Responses websocket extra headers must be a mapping.")

        handshake_headers = self._merge_websocket_headers(extra_headers_raw)
        ws_url = self._prepare_websocket_url(extra_query)

        frame: dict[str, Any] = {"type": "response.create"}
        for key, value in request_kwargs.items():
            if _is_openai_omitted_value(value):
                continue
            frame[key] = value

        frame["stream"] = True

        if extra_body is not None and not _is_openai_omitted_value(extra_body):
            if not isinstance(extra_body, Mapping):
                raise UserError("Responses websocket extra_body must be a mapping.")
            for key, value in extra_body.items():
                if _is_openai_omitted_value(value):
                    continue
                frame[str(key)] = value

        # Preserve websocket envelope fields regardless of `extra_body` contents.
        frame["type"] = "response.create"
        frame["stream"] = True

        return frame, ws_url, handshake_headers

    def _merge_websocket_headers(self, extra_headers: Mapping[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self._client.default_headers.items():
            if _is_openai_omitted_value(value):
                continue
            headers[key] = str(value)

        for key, value in extra_headers.items():
            if isinstance(value, NotGiven):
                continue
            header_key = str(key)
            for existing_key in list(headers):
                if existing_key.lower() == header_key.lower():
                    del headers[existing_key]
            if isinstance(value, Omit):
                continue
            headers[header_key] = str(value)

        return headers

    def _prepare_websocket_url(self, extra_query: Any) -> str:
        if self._client.websocket_base_url is not None:
            base_url = httpx.URL(self._client.websocket_base_url)
            ws_scheme = {"http": "ws", "https": "wss"}.get(base_url.scheme, base_url.scheme)
            base_url = base_url.copy_with(scheme=ws_scheme)
        else:
            client_base_url = self._client.base_url
            ws_scheme = {"http": "ws", "https": "wss"}.get(
                client_base_url.scheme, client_base_url.scheme
            )
            base_url = client_base_url.copy_with(scheme=ws_scheme)

        params: dict[str, Any] = dict(base_url.params)
        default_query = getattr(self._client, "default_query", None)
        if default_query is not None and not _is_openai_omitted_value(default_query):
            if not isinstance(default_query, Mapping):
                raise UserError("Responses websocket client default_query must be a mapping.")
            for key, value in default_query.items():
                query_key = str(key)
                if isinstance(value, Omit):
                    params.pop(query_key, None)
                    continue
                if isinstance(value, NotGiven):
                    continue
                params[query_key] = value

        if extra_query is not None and not _is_openai_omitted_value(extra_query):
            if not isinstance(extra_query, Mapping):
                raise UserError("Responses websocket extra_query must be a mapping.")
            for key, value in extra_query.items():
                query_key = str(key)
                if isinstance(value, Omit):
                    params.pop(query_key, None)
                    continue
                if isinstance(value, NotGiven):
                    continue
                params[query_key] = value

        path = base_url.path.rstrip("/") + "/responses"
        return str(base_url.copy_with(path=path, params=params))

    async def _ensure_websocket_connection(
        self,
        ws_url: str,
        headers: Mapping[str, str],
        *,
        connect_timeout: float | None,
    ) -> Any:
        running_loop = asyncio.get_running_loop()
        identity = (
            ws_url,
            tuple(sorted((str(key).lower(), str(value)) for key, value in headers.items())),
        )

        if self._ws_connection is not None and self._ws_connection_identity == identity:
            if (
                self._ws_connection_loop_ref is not None
                and self._ws_connection_loop_ref() is running_loop
                and self._is_websocket_connection_reusable(self._ws_connection)
            ):
                return self._ws_connection
        if self._ws_connection is not None:
            await self._drop_websocket_connection()
        self._ws_connection = await self._open_websocket_connection(
            ws_url,
            headers,
            connect_timeout=connect_timeout,
        )
        self._ws_connection_identity = identity
        self._ws_connection_loop_ref = weakref.ref(running_loop)
        return self._ws_connection

    def _is_websocket_connection_reusable(self, connection: Any) -> bool:
        try:
            state = getattr(connection, "state", None)
            state_name = getattr(state, "name", None)
            if isinstance(state_name, str):
                return state_name == "OPEN"

            closed = getattr(connection, "closed", None)
            if isinstance(closed, bool):
                return not closed

            is_open = getattr(connection, "open", None)
            if isinstance(is_open, bool):
                return is_open

            close_code = getattr(connection, "close_code", None)
            if close_code is not None:
                return False
        except Exception:
            return False

        return True

    async def close(self) -> None:
        """Close the persistent websocket connection, if one is open."""
        self._ws_client_close_generation += 1
        request_lock = self._get_current_loop_ws_request_lock()
        if request_lock is not None and request_lock.locked():
            if self._ws_connection is not None:
                self._force_abort_websocket_connection(self._ws_connection)
            self._clear_websocket_connection_state()
            return

        await self._drop_websocket_connection()

    def _get_current_loop_ws_request_lock(self) -> asyncio.Lock | None:
        if self._ws_request_lock is None or self._ws_request_lock_loop_ref is None:
            return None

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        if self._ws_request_lock_loop_ref() is not running_loop:
            return None

        return self._ws_request_lock

    def _force_abort_websocket_connection(self, connection: Any) -> None:
        """Best-effort fallback for cross-loop cleanup when awaiting close() fails."""
        try:
            transport = getattr(connection, "transport", None)
            if transport is not None:
                abort = getattr(transport, "abort", None)
                if callable(abort):
                    abort()
                    return
                close_transport = getattr(transport, "close", None)
                if callable(close_transport):
                    close_transport()
                    return
        except Exception:
            pass

    def _force_drop_websocket_connection_sync(self) -> None:
        """Synchronously abort and clear cached websocket state without awaiting close()."""
        self._ws_client_close_generation += 1
        if self._ws_connection is not None:
            self._force_abort_websocket_connection(self._ws_connection)
        self._clear_websocket_connection_state()
        # Also clear the loop-bound lock so closed-loop models don't retain stale lock state.
        self._ws_request_lock = None
        self._ws_request_lock_loop_ref = None

    def _clear_websocket_connection_state(self) -> None:
        """Clear cached websocket connection metadata."""
        self._ws_connection = None
        self._ws_connection_identity = None
        self._ws_connection_loop_ref = None

    async def _drop_websocket_connection(self) -> None:
        if self._ws_connection is None:
            self._clear_websocket_connection_state()
            return

        try:
            await self._ws_connection.close()
        except Exception:
            self._force_abort_websocket_connection(self._ws_connection)
        finally:
            self._clear_websocket_connection_state()

    async def _open_websocket_connection(
        self,
        ws_url: str,
        headers: Mapping[str, str],
        *,
        connect_timeout: float | None,
    ) -> Any:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise UserError(
                "OpenAIResponsesWSModel requires the `websockets` package. "
                "Install `websockets` or `openai[realtime]`."
            ) from exc

        return await connect(
            ws_url,
            user_agent_header=None,
            additional_headers=dict(headers),
            max_size=None,
            open_timeout=connect_timeout,
        )


@dataclass
class ConvertedTools:
    tools: list[ResponsesToolParam]
    includes: list[ResponseIncludable]


class Converter:
    @classmethod
    def _convert_shell_environment(cls, environment: ShellToolEnvironment | None) -> dict[str, Any]:
        """Convert shell environment settings to OpenAI payload shape."""
        if environment is None:
            return {"type": "local"}
        if not isinstance(environment, Mapping):
            raise UserError("Shell environment must be a mapping.")

        payload = dict(environment)
        if "type" not in payload:
            payload["type"] = "local"
        return payload

    @classmethod
    def convert_tool_choice(
        cls,
        tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None,
        *,
        tools: Sequence[Tool] | None = None,
        handoffs: Sequence[Handoff[Any, Any]] | None = None,
        model: str | ChatModel | None = None,
    ) -> response_create_params.ToolChoice | Omit:
        if tool_choice is None:
            return omit
        elif isinstance(tool_choice, MCPToolChoice):
            return {
                "server_label": tool_choice.server_label,
                "type": "mcp",
                "name": tool_choice.name,
            }
        elif tool_choice == "required":
            cls._validate_required_tool_choice(tools=tools)
            return "required"
        elif tool_choice == "auto":
            return "auto"
        elif tool_choice == "none":
            return "none"
        elif tool_choice == "file_search":
            return {
                "type": "file_search",
            }
        elif tool_choice == "web_search":
            return {
                # TODO: revisit the type: ignore comment when ToolChoice is updated in the future
                "type": "web_search",  # type: ignore[misc, return-value]
            }
        elif tool_choice == "web_search_preview":
            return {
                "type": "web_search_preview",
            }
        elif tool_choice in {
            "computer",
            "computer_use",
            "computer_use_preview",
        } and cls._has_computer_tool(tools):
            return cls._convert_builtin_computer_tool_choice(
                tool_choice=tool_choice,
                model=model,
            )
        elif tool_choice == "computer_use_preview":
            return {
                "type": "computer_use_preview",
            }
        elif tool_choice == "image_generation":
            return {
                "type": "image_generation",
            }
        elif tool_choice == "code_interpreter":
            return {
                "type": "code_interpreter",
            }
        elif tool_choice == "mcp":
            # Note that this is still here for backwards compatibility,
            # but migrating to MCPToolChoice is recommended.
            return {"type": "mcp"}  # type: ignore[misc, return-value]
        else:
            cls._validate_named_function_tool_choice(
                tool_choice,
                tools=tools,
                handoffs=handoffs,
            )
            return {
                "type": "function",
                "name": tool_choice,
            }

    @classmethod
    def _validate_required_tool_choice(
        cls,
        *,
        tools: Sequence[Tool] | None,
    ) -> None:
        """Reject required tool choice only when deferred tools cannot surface any tool call."""
        if not tools:
            return

        if any(isinstance(tool, ToolSearchTool) for tool in tools):
            return

        if has_required_tool_search_surface(list(tools)):
            raise UserError(
                "tool_choice='required' is not currently supported when deferred-loading "
                "Responses tools are configured without ToolSearchTool() on the OpenAI "
                "Responses API. Add ToolSearchTool() or use `auto`."
            )

    @classmethod
    def _validate_named_function_tool_choice(
        cls,
        tool_choice: str,
        *,
        tools: Sequence[Tool] | None,
        handoffs: Sequence[Handoff[Any, Any]] | None = None,
    ) -> None:
        """Reject named tool choices that would point at unsupported namespace surfaces."""
        if not tools and not handoffs:
            return

        top_level_function_names: set[str] = set()
        all_local_function_names: set[str] = set()
        deferred_only_function_names: set[str] = set()
        namespaced_function_names: set[str] = set()
        namespace_names: set[str] = set()
        has_hosted_tool_search = any(isinstance(tool, ToolSearchTool) for tool in tools or ())

        for handoff in handoffs or ():
            top_level_function_names.add(handoff.tool_name)
            all_local_function_names.add(handoff.tool_name)

        for tool in tools or ():
            if not isinstance(tool, FunctionTool):
                continue

            all_local_function_names.add(tool.name)
            explicit_namespace = get_explicit_function_tool_namespace(tool)
            if explicit_namespace is None:
                if tool.defer_loading:
                    deferred_only_function_names.add(tool.name)
                else:
                    top_level_function_names.add(tool.name)
                continue

            namespaced_function_names.add(tool.name)
            namespace_names.add(explicit_namespace)

        if (
            tool_choice == "tool_search"
            and has_hosted_tool_search
            and tool_choice not in all_local_function_names
        ):
            raise UserError(
                "tool_choice='tool_search' is not supported for ToolSearchTool() on the "
                "OpenAI Responses API. Use `auto` or `required`, or target a real "
                "top-level function tool named `tool_search`."
            )
        if (
            tool_choice == "tool_search"
            and not has_hosted_tool_search
            and tool_choice not in all_local_function_names
        ):
            raise UserError(
                "tool_choice='tool_search' requires ToolSearchTool() or a real top-level "
                "function tool named `tool_search` on the OpenAI Responses API."
            )
        if (
            tool_choice in namespaced_function_names and tool_choice not in top_level_function_names
        ) or (tool_choice in namespace_names and tool_choice not in top_level_function_names):
            raise UserError(
                "Named tool_choice must target a callable tool, not a namespace wrapper or "
                "bare inner name from tool_namespace(), on the OpenAI Responses API. Use "
                "`auto`, `required`, `none`, or target a top-level or qualified namespaced "
                "function tool."
            )
        if (
            tool_choice in deferred_only_function_names
            and tool_choice not in top_level_function_names
        ):
            raise UserError(
                "Named tool_choice is not currently supported for deferred-loading function "
                "tools on the OpenAI Responses API. Use `auto`, `required`, `none`, or load "
                "the tool via ToolSearchTool() first."
            )

    @classmethod
    def _has_computer_tool(cls, tools: Sequence[Tool] | None) -> bool:
        return any(isinstance(tool, ComputerTool) for tool in tools or ())

    @classmethod
    def _has_unresolved_computer_tool(cls, tools: Sequence[Tool] | None) -> bool:
        return any(
            isinstance(tool, ComputerTool)
            and not isinstance(tool.computer, Computer | AsyncComputer)
            for tool in tools or ()
        )

    @classmethod
    def _is_preview_computer_model(cls, model: str | ChatModel | None) -> bool:
        return isinstance(model, str) and model.startswith("computer-use-preview")

    @classmethod
    def _is_ga_computer_model(cls, model: str | ChatModel | None) -> bool:
        return isinstance(model, str) and (
            model.startswith("gpt-5.4") or model.startswith("gpt-5.5")
        )

    @classmethod
    def resolve_computer_tool_model(
        cls,
        *,
        request_model: str | ChatModel | None,
        tools: Sequence[Tool] | None,
    ) -> str | ChatModel | None:
        if not cls._has_computer_tool(tools):
            return None
        return request_model

    @classmethod
    def _should_use_preview_computer_tool(
        cls,
        *,
        model: str | ChatModel | None,
        tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None,
    ) -> bool:
        # Choose the computer tool wire shape from the effective request model when we know it.
        # For prompt-managed calls that omit `model`, default to the released preview payload
        # unless the caller explicitly opts into a GA computer-tool selector. The prompt may pin
        # a different model than the local default, so we must not infer the wire shape from
        # `self.model` when the request payload itself omits `model`.
        if cls._is_preview_computer_model(model):
            return True
        if model is not None:
            return False
        if isinstance(tool_choice, str) and tool_choice in {"computer", "computer_use"}:
            return False
        return True

    @classmethod
    def _convert_builtin_computer_tool_choice(
        cls,
        *,
        tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None,
        model: str | ChatModel | None,
    ) -> response_create_params.ToolChoice:
        # Preview models only support the preview computer tool selector, even if callers force
        # a GA-era alias such as "computer" or "computer_use".
        if cls._is_preview_computer_model(model):
            return {
                "type": "computer_use_preview",
            }
        if cls._should_use_preview_computer_tool(model=model, tool_choice=tool_choice):
            return {
                "type": "computer_use_preview",
            }
        # `computer_use` is a compatibility alias, but the GA built-in tool surface is `computer`.
        return {
            "type": "computer",
        }

    @classmethod
    def get_response_format(
        cls, output_schema: AgentOutputSchemaBase | None
    ) -> ResponseTextConfigParam | Omit:
        if output_schema is None or output_schema.is_plain_text():
            return omit
        else:
            return {
                "format": {
                    "type": "json_schema",
                    "name": "final_output",
                    "schema": output_schema.json_schema(),
                    "strict": output_schema.is_strict_json_schema(),
                }
            }

    @classmethod
    def convert_tools(
        cls,
        tools: list[Tool],
        handoffs: list[Handoff[Any, Any]],
        *,
        allow_opaque_tool_search_surface: bool = False,
        model: str | ChatModel | None = None,
        tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None = None,
    ) -> ConvertedTools:
        converted_tools: list[ResponsesToolParam | None] = []
        includes: list[ResponseIncludable] = []
        namespace_index_by_name: dict[str, int] = {}
        namespace_tools_by_name: dict[str, list[FunctionToolParam]] = {}
        namespace_descriptions: dict[str, str] = {}
        use_preview_computer_tool = cls._should_use_preview_computer_tool(
            model=model,
            tool_choice=tool_choice,
        )
        validate_responses_tool_search_configuration(
            tools,
            allow_opaque_search_surface=allow_opaque_tool_search_surface,
        )

        computer_tools = [tool for tool in tools if isinstance(tool, ComputerTool)]
        if len(computer_tools) > 1:
            raise UserError(f"You can only provide one computer tool. Got {len(computer_tools)}")

        for tool in tools:
            namespace_name = (
                get_explicit_function_tool_namespace(tool)
                if isinstance(tool, FunctionTool)
                else None
            )
            if isinstance(tool, FunctionTool) and namespace_name:
                if namespace_name not in namespace_index_by_name:
                    namespace_index_by_name[namespace_name] = len(converted_tools)
                    converted_tools.append(None)
                    namespace_tools_by_name[namespace_name] = []
                    namespace_descriptions[namespace_name] = (
                        get_function_tool_namespace_description(tool) or ""
                    )
                else:
                    expected_description = namespace_descriptions.get(namespace_name)
                    actual_description = get_function_tool_namespace_description(tool) or ""
                    if expected_description != actual_description:
                        raise UserError(
                            f"All tools in namespace '{namespace_name}' must share the same "
                            "description."
                        )

                converted_tool, include = cls._convert_function_tool(
                    tool,
                    include_defer_loading=True,
                )
                namespace_tools_by_name[namespace_name].append(converted_tool)
                if include:
                    includes.append(include)
                continue

            converted_non_namespace_tool, include = cls._convert_tool(
                tool,
                use_preview_computer_tool=use_preview_computer_tool,
            )
            converted_tools.append(converted_non_namespace_tool)
            if include:
                includes.append(include)

        for namespace_name, index in namespace_index_by_name.items():
            namespace_payload: _NamespaceToolParam = {
                "type": "namespace",
                "name": namespace_name,
                "description": namespace_descriptions[namespace_name],
                "tools": namespace_tools_by_name[namespace_name],
            }
            converted_tools[index] = _require_responses_tool_param(namespace_payload)

        for handoff in handoffs:
            converted_tools.append(cls._convert_handoff_tool(handoff))

        return ConvertedTools(
            tools=[tool for tool in converted_tools if tool is not None],
            includes=includes,
        )

    @classmethod
    def _convert_function_tool(
        cls,
        tool: FunctionTool,
        *,
        include_defer_loading: bool = True,
    ) -> tuple[FunctionToolParam, ResponseIncludable | None]:
        function_tool_param: FunctionToolParam = {
            "name": tool.name,
            "parameters": tool.params_json_schema,
            "strict": tool.strict_json_schema,
            "type": "function",
            "description": tool.description,
        }
        if include_defer_loading and tool.defer_loading:
            function_tool_param["defer_loading"] = True
        return function_tool_param, None

    @classmethod
    def _convert_preview_computer_tool(cls, tool: ComputerTool[Any]) -> ResponsesToolParam:
        computer = tool.computer
        if not isinstance(computer, Computer | AsyncComputer):
            raise UserError(
                "Computer tool is not initialized for serialization. Call "
                "resolve_computer({ tool, run_context }) with a run context first "
                "when building payloads manually."
            )
        environment = computer.environment
        dimensions = computer.dimensions
        if environment is None or dimensions is None:
            raise UserError(
                "Preview computer tool payloads require `environment` and `dimensions` on the "
                "Computer/AsyncComputer implementation."
            )
        return _require_responses_tool_param(
            {
                "type": "computer_use_preview",
                "environment": environment,
                "display_width": dimensions[0],
                "display_height": dimensions[1],
            }
        )

    @classmethod
    def _convert_tool(
        cls,
        tool: Tool,
        *,
        use_preview_computer_tool: bool = False,
    ) -> tuple[ResponsesToolParam, ResponseIncludable | None]:
        """Returns converted tool and includes"""

        if isinstance(tool, FunctionTool):
            return cls._convert_function_tool(tool)
        elif isinstance(tool, WebSearchTool):
            web_search_tool: dict[str, Any] = {
                "type": "web_search",
                "filters": tool.filters.model_dump() if tool.filters is not None else None,
                "user_location": tool.user_location,
                "search_context_size": tool.search_context_size,
            }
            if tool.external_web_access is not None:
                web_search_tool["external_web_access"] = tool.external_web_access
            return (
                _require_responses_tool_param(web_search_tool),
                None,
            )
        elif isinstance(tool, FileSearchTool):
            file_search_tool_param: FileSearchToolParam = {
                "type": "file_search",
                "vector_store_ids": tool.vector_store_ids,
            }
            if tool.max_num_results:
                file_search_tool_param["max_num_results"] = tool.max_num_results
            if tool.ranking_options:
                file_search_tool_param["ranking_options"] = tool.ranking_options
            if tool.filters:
                file_search_tool_param["filters"] = tool.filters

            include: ResponseIncludable | None = (
                "file_search_call.results" if tool.include_search_results else None
            )
            return file_search_tool_param, include
        elif isinstance(tool, ComputerTool):
            return (
                cls._convert_preview_computer_tool(tool)
                if use_preview_computer_tool
                else _require_responses_tool_param({"type": "computer"}),
                None,
            )
        elif isinstance(tool, CustomTool):
            custom_tool_param: CustomToolParam = tool.tool_config
            return custom_tool_param, None
        elif isinstance(tool, HostedMCPTool):
            return tool.tool_config, None
        elif isinstance(tool, ApplyPatchTool):
            tool_config = getattr(tool, "tool_config", None)
            if tool_config is not None:
                return _require_responses_tool_param(tool_config), None
            return ApplyPatchToolParam(type="apply_patch"), None
        elif isinstance(tool, ShellTool):
            return (
                _require_responses_tool_param(
                    {
                        "type": "shell",
                        "environment": cls._convert_shell_environment(tool.environment),
                    }
                ),
                None,
            )
        elif isinstance(tool, ImageGenerationTool):
            return tool.tool_config, None
        elif isinstance(tool, CodeInterpreterTool):
            return tool.tool_config, None
        elif isinstance(tool, LocalShellTool):
            return LocalShell(type="local_shell"), None
        elif isinstance(tool, ToolSearchTool):
            tool_search_tool_param = ToolSearchToolParam(type="tool_search")
            if isinstance(tool.description, str):
                tool_search_tool_param["description"] = tool.description
            if tool.execution is not None:
                tool_search_tool_param["execution"] = tool.execution
            if tool.parameters is not None:
                tool_search_tool_param["parameters"] = tool.parameters
            return tool_search_tool_param, None
        else:
            raise UserError(f"Unknown tool type: {type(tool)}, tool")

    @classmethod
    def _convert_handoff_tool(cls, handoff: Handoff) -> ResponsesToolParam:
        return FunctionToolParam(
            name=handoff.tool_name,
            parameters=handoff.input_json_schema,
            strict=handoff.strict_json_schema,
            type="function",
            description=handoff.tool_description,
        )
