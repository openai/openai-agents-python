from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Literal, Union, overload

from openai.types.responses.file_search_tool_param import Filters, RankingOptions
from openai.types.responses.web_search_tool_param import UserLocation
from pydantic import ValidationError
from typing_extensions import Concatenate, ParamSpec

from . import _debug, _utils
from ._utils import MaybeAwaitable
from .computer import AsyncComputer, Computer
from .exceptions import ModelBehaviorError
from .function_schema import DocstringStyle, function_schema
from .logger import logger
from .run_context import RunContextWrapper
from .tracing import SpanError

ToolParams = ParamSpec("ToolParams")

ToolFunctionWithoutContext = Callable[ToolParams, Any]
ToolFunctionWithContext = Callable[Concatenate[RunContextWrapper[Any], ToolParams], Any]

ToolFunction = Union[ToolFunctionWithoutContext[ToolParams], ToolFunctionWithContext[ToolParams]]


@dataclass
class FunctionTool:
    """A tool that wraps a function. In most cases, you should use  the `function_tool` helpers to
    create a FunctionTool, as they let you easily wrap a Python function.
    """

    name: str
    """The name of the tool, as shown to the LLM. Generally the name of the function."""

    description: str
    """A description of the tool, as shown to the LLM."""

    params_json_schema: dict[str, Any]
    """The JSON schema for the tool's parameters."""

    on_invoke_tool: Callable[[RunContextWrapper[Any], str], Awaitable[str]]
    """A function that invokes the tool with the given context and parameters. The params passed
    are:
    1. The tool run context.
    2. The arguments from the LLM, as a JSON string.

    You must return a string representation of the tool output. In case of errors, you can either
    raise an Exception (which will cause the run to fail) or return a string error message (which
    will be sent back to the LLM).
    """

    strict_json_schema: bool = True
    """Whether the JSON schema is in strict mode. We **strongly** recommend setting this to True,
    as it increases the likelihood of correct JSON input."""


@dataclass
class FileSearchTool:
    """A hosted tool that lets the LLM search through a vector store. Currently only supported with
    OpenAI models, using the Responses API.
    """

    vector_store_ids: list[str]
    """The IDs of the vector stores to search."""

    max_num_results: int | None = None
    """The maximum number of results to return."""

    include_search_results: bool = False
    """Whether to include the search results in the output produced by the LLM."""

    ranking_options: RankingOptions | None = None
    """Ranking options for search."""

    filters: Filters | None = None
    """A filter to apply based on file attributes."""

    @property
    def name(self):
        return "file_search"


@dataclass
class WebSearchTool:
    """A hosted tool that lets the LLM search the web. Currently only supported with OpenAI models,
    using the Responses API.
    """

    user_location: UserLocation | None = None
    """Optional location for the search. Lets you customize results to be relevant to a location."""

    search_context_size: Literal["low", "medium", "high"] = "medium"
    """The amount of context to use for the search."""

    @property
    def name(self):
        return "web_search_preview"


@dataclass
class ComputerTool:
    """A hosted tool that lets the LLM control a computer."""

    computer: Computer | AsyncComputer
    """The computer implementation, which describes the environment and dimensions of the computer,
    as well as implements the computer actions like click, screenshot, etc.
    """

    @property
    def name(self):
        return "computer_use_preview"


Tool = Union[FunctionTool, FileSearchTool, WebSearchTool, ComputerTool]
"""A tool that can be used in an agent."""


def default_tool_error_function(ctx: RunContextWrapper[Any], error: Exception) -> str:
    """The default tool error function, which just returns a generic error message."""
    return f"An error occurred while running the tool. Please try again. Error: {str(error)}"


ToolErrorFunction = Callable[[RunContextWrapper[Any], Exception], MaybeAwaitable[str]]


@overload
def function_tool(
    func: ToolFunction[...],
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = None,
) -> FunctionTool:
    """Overload for usage as @function_tool (no parentheses)."""
    ...


@overload
def function_tool(
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = None,
) -> Callable[[ToolFunction[...]], FunctionTool]:
    """Overload for usage as @function_tool(...)."""
    ...


def function_tool(
    func: ToolFunction[...] | None = None,
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = default_tool_error_function,
) -> FunctionTool | Callable[[ToolFunction[...]], FunctionTool]:
    """
    Decorator to create a FunctionTool from a function. By default, we will:
    1. Parse the function signature to create a JSON schema for the tool's parameters.
    2. Use the function's docstring to populate the tool's description.
    3. Use the function's docstring to populate argument descriptions.
    The docstring style is detected automatically, but you can override it.

    If the function takes a `RunContextWrapper` as the first argument, it *must* match the
    context type of the agent that uses the tool.

    Args:
        func: The function to wrap.
        name_override: If provided, use this name for the tool instead of the function's name.
        description_override: If provided, use this description for the tool instead of the
            function's docstring.
        docstring_style: If provided, use this style for the tool's docstring. If not provided,
            we will attempt to auto-detect the style.
        use_docstring_info: If True, use the function's docstring to populate the tool's
            description and argument descriptions.
        failure_error_function: If provided, use this function to generate an error message when
            the tool call fails. The error message is sent to the LLM. If you pass None, then no
            error message will be sent and instead an Exception will be raised.
    """

    def _create_function_tool(the_func: ToolFunction[...]) -> FunctionTool:
        schema = function_schema(
            func=the_func,
            name_override=name_override,
            description_override=description_override,
            docstring_style=docstring_style,
            use_docstring_info=use_docstring_info,
        )

        async def _on_invoke_tool_impl(ctx: RunContextWrapper[Any], input: str) -> str:
            try:
                json_data: dict[str, Any] = json.loads(input) if input else {}
            except Exception as e:
                if _debug.DONT_LOG_TOOL_DATA:
                    logger.debug(f"Invalid JSON input for tool {schema.name}")
                else:
                    logger.debug(f"Invalid JSON input for tool {schema.name}: {input}")
                raise ModelBehaviorError(
                    f"Invalid JSON input for tool {schema.name}: {input}"
                ) from e

            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Invoking tool {schema.name}")
            else:
                logger.debug(f"Invoking tool {schema.name} with input {input}")

            try:
                parsed = (
                    schema.params_pydantic_model(**json_data)
                    if json_data
                    else schema.params_pydantic_model()
                )
            except ValidationError as e:
                raise ModelBehaviorError(f"Invalid JSON input for tool {schema.name}: {e}") from e

            args, kwargs_dict = schema.to_call_args(parsed)

            if not _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Tool call args: {args}, kwargs: {kwargs_dict}")

            if inspect.iscoroutinefunction(the_func):
                if schema.takes_context:
                    result = await the_func(ctx, *args, **kwargs_dict)
                else:
                    result = await the_func(*args, **kwargs_dict)
            else:
                if schema.takes_context:
                    result = the_func(ctx, *args, **kwargs_dict)
                else:
                    result = the_func(*args, **kwargs_dict)

            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Tool {schema.name} completed.")
            else:
                logger.debug(f"Tool {schema.name} returned {result}")

            return str(result)

        async def _on_invoke_tool(ctx: RunContextWrapper[Any], input: str) -> str:
            try:
                return await _on_invoke_tool_impl(ctx, input)
            except Exception as e:
                if failure_error_function is None:
                    raise

                result = failure_error_function(ctx, e)
                if inspect.isawaitable(result):
                    return await result

                _utils.attach_error_to_current_span(
                    SpanError(
                        message="Error running tool (non-fatal)",
                        data={
                            "tool_name": schema.name,
                            "error": str(e),
                        },
                    )
                )
                return result

        return FunctionTool(
            name=schema.name,
            description=schema.description or "",
            params_json_schema=schema.params_json_schema,
            on_invoke_tool=_on_invoke_tool,
        )

    # If func is actually a callable, we were used as @function_tool with no parentheses
    if callable(func):
        return _create_function_tool(func)

    # Otherwise, we were used as @function_tool(...), so return a decorator
    def decorator(real_func: ToolFunction[...]) -> FunctionTool:
        return _create_function_tool(real_func)

    return decorator
