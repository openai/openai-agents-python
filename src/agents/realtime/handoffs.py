from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, cast, overload

from pydantic import TypeAdapter
from typing_extensions import TypeVar

from ..exceptions import ModelBehaviorError, UserError
from ..handoffs import Handoff
from ..run_context import RunContextWrapper, TContext
from ..strict_schema import ensure_strict_json_schema
from ..tracing.spans import SpanError
from ..util import _error_tracing, _json
from ..util._types import MaybeAwaitable

if TYPE_CHECKING:
    from ..agent import AgentBase
    from . import RealtimeAgent


# The handoff input type is the type of data passed when the agent is called via a handoff.
THandoffInput = TypeVar("THandoffInput", default=Any)

OnHandoffWithInput = Callable[[RunContextWrapper[Any], THandoffInput], Any]
OnHandoffWithoutInput = Callable[[RunContextWrapper[Any]], Any]


@overload
def realtime_handoff(
    agent: RealtimeAgent[TContext],
    *,
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], RealtimeAgent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, RealtimeAgent[TContext]]: ...


@overload
def realtime_handoff(
    agent: RealtimeAgent[TContext],
    *,
    on_handoff: OnHandoffWithInput[THandoffInput],
    input_type: type[THandoffInput],
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], RealtimeAgent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, RealtimeAgent[TContext]]: ...


@overload
def realtime_handoff(
    agent: RealtimeAgent[TContext],
    *,
    on_handoff: OnHandoffWithoutInput,
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], RealtimeAgent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, RealtimeAgent[TContext]]: ...


def realtime_handoff(
    agent: RealtimeAgent[TContext],
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    on_handoff: OnHandoffWithInput[THandoffInput] | OnHandoffWithoutInput | None = None,
    input_type: type[THandoffInput] | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], RealtimeAgent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, RealtimeAgent[TContext]]:
    """Create a handoff from a RealtimeAgent.

    Args:
        agent: The RealtimeAgent to handoff to, or a function that returns a RealtimeAgent.
        tool_name_override: Optional override for the name of the tool that represents the handoff.
        tool_description_override: Optional override for the description of the tool that
            represents the handoff.
        on_handoff: A function that runs when the handoff is invoked.
        input_type: the type of the input to the handoff. If provided, the input will be validated
            against this type. Only relevant if you pass a function that takes an input.
        is_enabled: Whether the handoff is enabled. Can be a bool or a callable that takes the run
            context and agent and returns whether the handoff is enabled. Disabled handoffs are
            hidden from the LLM at runtime.

    Note: input_filter is not supported for RealtimeAgent handoffs.
    """
    assert (on_handoff and input_type) or not (on_handoff and input_type), (
        "You must provide either both on_handoff and input_type, or neither"
    )
    type_adapter: TypeAdapter[Any] | None
    if input_type is not None:
        assert callable(on_handoff), "on_handoff must be callable"
        sig = inspect.signature(on_handoff)
        if len(sig.parameters) != 2:
            raise UserError("on_handoff must take two arguments: context and input")

        type_adapter = TypeAdapter(input_type)
        input_json_schema = type_adapter.json_schema()
    else:
        type_adapter = None
        input_json_schema = {}
        if on_handoff is not None:
            sig = inspect.signature(on_handoff)
            if len(sig.parameters) != 1:
                raise UserError("on_handoff must take one argument: context")

    async def _invoke_handoff(
        ctx: RunContextWrapper[Any], input_json: str | None = None
    ) -> RealtimeAgent[TContext]:
        if input_type is not None and type_adapter is not None:
            if input_json is None:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Handoff function expected non-null input, but got None",
                        data={"details": "input_json is None"},
                    )
                )
                raise ModelBehaviorError("Handoff function expected non-null input, but got None")

            validated_input = _json.validate_json(
                json_str=input_json,
                type_adapter=type_adapter,
                partial=False,
            )
            input_func = cast(OnHandoffWithInput[THandoffInput], on_handoff)
            if inspect.iscoroutinefunction(input_func):
                await input_func(ctx, validated_input)
            else:
                input_func(ctx, validated_input)
        elif on_handoff is not None:
            no_input_func = cast(OnHandoffWithoutInput, on_handoff)
            if inspect.iscoroutinefunction(no_input_func):
                await no_input_func(ctx)
            else:
                no_input_func(ctx)

        return agent

    tool_name = tool_name_override or Handoff.default_tool_name(agent)
    tool_description = tool_description_override or Handoff.default_tool_description(agent)

    # Always ensure the input JSON schema is in strict mode
    # If there is a need, we can make this configurable in the future
    input_json_schema = ensure_strict_json_schema(input_json_schema)

    async def _is_enabled(ctx: RunContextWrapper[Any], agent_base: AgentBase[Any]) -> bool:
        assert callable(is_enabled), "is_enabled must be non-null here"
        assert isinstance(agent_base, RealtimeAgent), "Can't handoff to a non-RealtimeAgent"
        result = is_enabled(ctx, agent_base)
        if inspect.isawaitable(result):
            return await result
        return result

    return Handoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=input_json_schema,
        on_invoke_handoff=_invoke_handoff,
        input_filter=None,  # Not supported for RealtimeAgent handoffs
        agent_name=agent.name,
        is_enabled=_is_enabled if callable(is_enabled) else is_enabled,
    )
