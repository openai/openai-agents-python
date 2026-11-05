"""Detached native sampling values for an operation's prepared-request callback."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from .tools import ToolRef
    from .turn import Turn


class PreparedToolPresentation(TypedDict, closed=True):
    """A selected native function leaf; edit only description and parameters.

    Ordinary MCP leaves retain their captured namespace/name. Sampled schema
    changes preserve existing execution validators and approval policies;
    ordinary MCP does not gain automatic argument-schema validation.
    """

    namespace: str | None
    name: str
    kind: Literal["function"]
    strict: bool
    defer_loading: bool | None
    description: str
    parameters: dict[str, Any]


class PreparedRequest(TypedDict, closed=True):
    """A detached sampling copy, separate from canonical conversation history.

    Input uses native ResponseItem JSON, including string function arguments.
    Omit optional fields that native serialization skips; explicit nulls that
    disappear and unsupported schema keywords are rejected before dispatch.
    Return the complete value or None to use the original request. Normal
    native transport formatting follows the callback.

    If automatic compaction is due before or during sampling, the turn fails
    before sending the compaction request. Shorten the input or start a new
    operation without the editor. Earlier tool effects still apply. If
    compaction is due after the turn completes, the caller receives the answer.
    """

    instructions: str
    input: list[dict[str, Any]]
    tools: list[PreparedToolPresentation]


class _PrepareRequest(Protocol):
    def __call__(
        self,
        turn: Turn[Any],
        request: PreparedRequest,
        /,
        *,
        preparation_index: int,
        recovery_attempt: int,
    ) -> PreparedRequest | None | Awaitable[PreparedRequest | None]: ...


def _capture(value: Any) -> str | None:
    if value is None:
        return None

    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("prepare_request object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise TypeError("prepare_request must return JSON data")

    if not isinstance(value, dict):
        raise TypeError("prepare_request must return a complete PreparedRequest or None")
    check(value)
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise TypeError("prepare_request must return finite JSON data") from None


def _capturing_callback(
    callback: Callable[..., Any], capture_allowed: Callable[[], bool]
) -> Callable[..., Any]:
    # Capture on the callback's executor immediately on settlement, before
    # handing its retained mutable result back to the application event loop.
    if inspect.iscoroutinefunction(callback):

        async def asynchronous(*args: Any, **kwargs: Any) -> str | None:
            value = await callback(*args, **kwargs)
            return _capture(value) if capture_allowed() else None

        return asynchronous

    def synchronous(*args: Any, **kwargs: Any) -> Any:
        value = callback(*args, **kwargs)
        if inspect.isawaitable(value):

            async def settle() -> str | None:
                result = await value
                return _capture(result) if capture_allowed() else None

            return settle()
        return _capture(value) if capture_allowed() else None

    return synchronous


def _check_profile(agent: Any, *, websockets: bool, environment: Any) -> None:
    if websockets:
        raise ValueError(
            "prepare_request requires a standard Responses (non-Lite) model and HTTP; "
            "select a standard model such as gpt-5.5 and NativeProvider(supports_websockets=False)"
        )
    if environment is not None or agent.programmatic_tool_calling:
        raise ValueError(
            "prepare_request supports direct-root eager function/MCP tools "
            "without an Environment or code mode"
        )


def _check_tools(tools: Sequence[Any], refs: Sequence[ToolRef] | None = None) -> None:
    from ._function_tools import FunctionTool
    from .mcp import HttpMCP, StdioMCP

    unsupported = (
        "prepare_request supports only ordinary eager function/MCP tools, "
        "without children or hosted tools"
    )
    allowed: set[ToolRef] = set()
    for tool in tools:
        if isinstance(tool, FunctionTool):
            allowed.add(tool.ref)
        elif type(tool) in (HttpMCP, StdioMCP):
            allowed.update(tool.tool(name) for name in tool.tools)
        else:
            raise ValueError(unsupported)
        if tool.defer_loading:
            raise ValueError("prepare_request requires eager tools; set defer_loading=False")
    if refs is not None and not allowed.issuperset(refs):
        raise ValueError(unsupported)


def _check_output(output: Any) -> None:
    if output.max_retries:
        raise ValueError(
            "prepare_request does not support separate output correction attempts; "
            "set PromptedOutput(max_retries=0) or use NativeOutput"
        )
