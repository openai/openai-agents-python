from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Union, cast, overload

from openai import APIStatusError, AsyncOpenAI, AsyncStream, Omit, omit
from openai.types import ChatModel
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseIncludable,
    ResponseStreamEvent,
    ResponseTextConfigParam,
    ToolParam,
    response_create_params,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam

from .. import _debug
from ..agent_output import AgentOutputSchemaBase
from ..exceptions import UserError
from ..handoffs import Handoff
from ..items import ItemHelpers, ModelResponse, TResponseInputItem
from ..logger import logger
from ..model_settings import MCPToolChoice
from ..tool import (
    CodeInterpreterTool,
    ComputerTool,
    FileSearchTool,
    FunctionTool,
    HostedMCPTool,
    ImageGenerationTool,
    LocalShellTool,
    Tool,
    WebSearchTool,
)
from ..tracing import SpanError, response_span
from ..usage import Usage
from ..util._json import _to_dump_compatible
from ..version import __version__
from .interface import Model, ModelTracing

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


_USER_AGENT = f"Agents/Python {__version__}"
_HEADERS = {"User-Agent": _USER_AGENT}

# Override headers used by the Responses API.
_HEADERS_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar(
    "openai_responses_headers_override", default=None
)


class OpenAIResponsesModel(Model):
    """
    Implementation of `Model` that uses the OpenAI Responses API.
    """

    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
    ) -> None:
        self.model = model
        self._client = openai_client

    def _non_null_or_omit(self, value: Any) -> Any:
        return value if value is not None else omit

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
                request_id = e.request_id if isinstance(e, APIStatusError) else None
                logger.error(f"Error getting response: {e}. (request_id: {request_id})")
                raise

        return ModelResponse(
            output=response.output,
            usage=usage,
            response_id=response.id,
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

                async for chunk in stream:
                    if isinstance(chunk, ResponseCompletedEvent):
                        final_response = chunk.response
                    yield chunk

                if final_response and tracing.include_data():
                    span_response.span_data.response = final_response
                    span_response.span_data.input = input

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
    ) -> AsyncStream[ResponseStreamEvent]: ...

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
    ) -> Response | AsyncStream[ResponseStreamEvent]:
        list_input = ItemHelpers.input_to_new_input_list(input)
        list_input = _to_dump_compatible(list_input)

        if model_settings.parallel_tool_calls and tools:
            parallel_tool_calls: bool | Omit = True
        elif model_settings.parallel_tool_calls is False:
            parallel_tool_calls = False
        else:
            parallel_tool_calls = omit

        tool_choice = Converter.convert_tool_choice(model_settings.tool_choice)
        converted_tools = Converter.convert_tools(tools, handoffs)
        converted_tools_payload = _to_dump_compatible(converted_tools.tools)
        response_format = Converter.get_response_format(output_schema)

        include_set: set[str] = set(converted_tools.includes)
        if model_settings.response_include is not None:
            include_set.update(model_settings.response_include)
        if model_settings.top_logprobs is not None:
            include_set.add("message.output_text.logprobs")
        include = cast(list[ResponseIncludable], list(include_set))

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
                f"Tool choice: {tool_choice}\n"
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

        response = await self._client.responses.create(
            previous_response_id=self._non_null_or_omit(previous_response_id),
            conversation=self._non_null_or_omit(conversation_id),
            instructions=self._non_null_or_omit(system_instructions),
            model=self.model,
            input=list_input,
            include=include,
            tools=converted_tools_payload,
            prompt=self._non_null_or_omit(prompt),
            temperature=self._non_null_or_omit(model_settings.temperature),
            top_p=self._non_null_or_omit(model_settings.top_p),
            truncation=self._non_null_or_omit(model_settings.truncation),
            max_output_tokens=self._non_null_or_omit(model_settings.max_tokens),
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            stream=cast(Any, stream_param),
            extra_headers=self._merge_headers(model_settings),
            extra_query=model_settings.extra_query,
            extra_body=model_settings.extra_body,
            text=response_format,
            store=self._non_null_or_omit(model_settings.store),
            reasoning=self._non_null_or_omit(model_settings.reasoning),
            metadata=self._non_null_or_omit(model_settings.metadata),
            **extra_args,
        )
        return cast(Union[Response, AsyncStream[ResponseStreamEvent]], response)

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI()
        return self._client

    def _merge_headers(self, model_settings: ModelSettings):
        return {
            **_HEADERS,
            **(model_settings.extra_headers or {}),
            **(_HEADERS_OVERRIDE.get() or {}),
        }


@dataclass
class ConvertedTools:
    tools: list[ToolParam]
    includes: list[ResponseIncludable]


class Converter:
    @classmethod
    def convert_tool_choice(
        cls, tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None
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
                # TODO: revist the type: ignore comment when ToolChoice is updated in the future
                "type": "web_search",  # type: ignore [typeddict-item]
            }
        elif tool_choice == "web_search_preview":
            return {
                "type": "web_search_preview",
            }
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
            return {"type": "mcp"}  # type: ignore [typeddict-item]
        else:
            return {
                "type": "function",
                "name": tool_choice,
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
    ) -> ConvertedTools:
        converted_tools: list[ToolParam] = []
        includes: list[ResponseIncludable] = []

        computer_tools = [tool for tool in tools if isinstance(tool, ComputerTool)]
        if len(computer_tools) > 1:
            raise UserError(f"You can only provide one computer tool. Got {len(computer_tools)}")

        for tool in tools:
            converted_tool, include = cls._convert_tool(tool)
            converted_tools.append(converted_tool)
            if include:
                includes.append(include)

        for handoff in handoffs:
            converted_tools.append(cls._convert_handoff_tool(handoff))

        return ConvertedTools(tools=converted_tools, includes=includes)

    @classmethod
    def _convert_tool(cls, tool: Tool) -> tuple[ToolParam, ResponseIncludable | None]:
        """Returns converted tool and includes"""

        if isinstance(tool, FunctionTool):
            converted_tool: ToolParam = {
                "name": tool.name,
                "parameters": tool.params_json_schema,
                "strict": tool.strict_json_schema,
                "type": "function",
                "description": tool.description,
            }
            includes: ResponseIncludable | None = None
        elif isinstance(tool, WebSearchTool):
            # TODO: revist the type: ignore comment when ToolParam is updated in the future
            converted_tool = {
                "type": "web_search",
                "filters": tool.filters.model_dump() if tool.filters is not None else None,  # type: ignore [typeddict-item]
                "user_location": tool.user_location,
                "search_context_size": tool.search_context_size,
            }
            includes = None
        elif isinstance(tool, FileSearchTool):
            converted_tool = {
                "type": "file_search",
                "vector_store_ids": tool.vector_store_ids,
            }
            if tool.max_num_results:
                converted_tool["max_num_results"] = tool.max_num_results
            if tool.ranking_options:
                converted_tool["ranking_options"] = tool.ranking_options
            if tool.filters:
                converted_tool["filters"] = tool.filters

            includes = "file_search_call.results" if tool.include_search_results else None
        elif isinstance(tool, ComputerTool):
            converted_tool = {
                "type": "computer_use_preview",
                "environment": tool.computer.environment,
                "display_width": tool.computer.dimensions[0],
                "display_height": tool.computer.dimensions[1],
            }
            includes = None
        elif isinstance(tool, HostedMCPTool):
            converted_tool = tool.tool_config
            includes = None
        elif isinstance(tool, ImageGenerationTool):
            converted_tool = tool.tool_config
            includes = None
        elif isinstance(tool, CodeInterpreterTool):
            converted_tool = tool.tool_config
            includes = None
        elif isinstance(tool, LocalShellTool):
            converted_tool = {
                "type": "local_shell",
            }
            includes = None
        else:
            raise UserError(f"Unknown tool type: {type(tool)}, tool")

        return converted_tool, includes

    @classmethod
    def _convert_handoff_tool(cls, handoff: Handoff) -> ToolParam:
        return {
            "name": handoff.tool_name,
            "parameters": handoff.input_json_schema,
            "strict": handoff.strict_json_schema,
            "type": "function",
            "description": handoff.tool_description,
        }
