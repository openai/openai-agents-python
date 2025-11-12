from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass, replace as dataclasses_replace
from typing import TYPE_CHECKING, Any, Callable, Generic, cast, overload

from pydantic import TypeAdapter
from typing_extensions import TypeAlias, TypeVar

from ..exceptions import ModelBehaviorError, UserError
from ..items import RunItem, TResponseInputItem
from ..run_context import RunContextWrapper, TContext
from ..strict_schema import ensure_strict_json_schema
from ..tracing.spans import SpanError
from ..util import _error_tracing, _json, _transforms
from ..util._types import MaybeAwaitable
from .history import (
    default_handoff_history_mapper,
    get_conversation_history_wrappers,
    nest_handoff_history,
    reset_conversation_history_wrappers,
    set_conversation_history_wrappers,
)

if TYPE_CHECKING:
    from ..agent import Agent, AgentBase


THandoffInput = TypeVar("THandoffInput", default=Any)
TAgent = TypeVar("TAgent", bound="AgentBase[Any]", default="Agent[Any]")

OnHandoffWithInput = Callable[[RunContextWrapper[Any], THandoffInput], Any]
OnHandoffWithoutInput = Callable[[RunContextWrapper[Any]], Any]


@dataclass(frozen=True)
class HandoffInputData:
    input_history: str | tuple[TResponseInputItem, ...]
    pre_handoff_items: tuple[RunItem, ...]
    new_items: tuple[RunItem, ...]
    run_context: RunContextWrapper[Any] | None = None

    def clone(self, **kwargs: Any) -> HandoffInputData:
        return dataclasses_replace(self, **kwargs)


HandoffInputFilter: TypeAlias = Callable[[HandoffInputData], MaybeAwaitable[HandoffInputData]]
HandoffHistoryMapper: TypeAlias = Callable[[list[TResponseInputItem]], list[TResponseInputItem]]


@dataclass
class Handoff(Generic[TContext, TAgent]):
    tool_name: str
    tool_description: str
    input_json_schema: dict[str, Any]
    on_invoke_handoff: Callable[[RunContextWrapper[Any], str], Awaitable[TAgent]]
    agent_name: str
    input_filter: HandoffInputFilter | None = None
    nest_handoff_history: bool | None = None
    strict_json_schema: bool = True
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase[Any]], MaybeAwaitable[bool]] = (
        True
    )

    def get_transfer_message(self, agent: AgentBase[Any]) -> str:
        return json.dumps({"assistant": agent.name})

    @classmethod
    def default_tool_name(cls, agent: AgentBase[Any]) -> str:
        return _transforms.transform_string_function_style(f"transfer_to_{agent.name}")

    @classmethod
    def default_tool_description(cls, agent: AgentBase[Any]) -> str:
        return (
            f"Handoff to the {agent.name} agent to handle the request. "
            f"{agent.handoff_description or ''}"
        )


@overload
def handoff(
    agent: Agent[TContext],
    *,
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


@overload
def handoff(
    agent: Agent[TContext],
    *,
    on_handoff: OnHandoffWithInput[THandoffInput],
    input_type: type[THandoffInput],
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


@overload
def handoff(
    agent: Agent[TContext],
    *,
    on_handoff: OnHandoffWithoutInput,
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


def handoff(
    agent: Agent[TContext],
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    on_handoff: OnHandoffWithInput[THandoffInput] | OnHandoffWithoutInput | None = None,
    input_type: type[THandoffInput] | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], Agent[TContext]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]:
    assert (on_handoff and input_type) or not (
        on_handoff and input_type
    ), "You must provide either both on_handoff and input_type, or neither"
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
    ) -> Agent[TContext]:
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
    input_json_schema = ensure_strict_json_schema(input_json_schema)

    async def _is_enabled(ctx: RunContextWrapper[Any], agent_base: AgentBase[Any]) -> bool:
        from ..agent import Agent

        assert callable(is_enabled), "is_enabled must be callable here"
        assert isinstance(agent_base, Agent), "Can't handoff to a non-Agent"
        result = is_enabled(ctx, agent_base)
        if inspect.isawaitable(result):
            return await result
        return bool(result)

    return Handoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=input_json_schema,
        on_invoke_handoff=_invoke_handoff,
        input_filter=input_filter,
        nest_handoff_history=nest_handoff_history,
        agent_name=agent.name,
        is_enabled=_is_enabled if callable(is_enabled) else is_enabled,
    )


__all__ = [
    "Handoff",
    "HandoffHistoryMapper",
    "HandoffInputData",
    "HandoffInputFilter",
    "default_handoff_history_mapper",
    "get_conversation_history_wrappers",
    "handoff",
    "nest_handoff_history",
    "reset_conversation_history_wrappers",
    "set_conversation_history_wrappers",
]
