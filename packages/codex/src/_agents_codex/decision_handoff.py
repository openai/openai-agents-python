"""Captured Decisions selection for a native transfer invoked by the model."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ._function_tools import FunctionTool, ToolContext
from ._public import preserve_module_identity as _preserve_module_identity
from .tools import _CapturedFunction

if TYPE_CHECKING:
    from .agent import Agent


@dataclass(frozen=True, kw_only=True)
class DecisionHandoff:
    """The main model has been invoking one captured Decisions routing tool.

    Named targets have been declaring the authorized roster. Decisions has been
    receiving only the model's bounded routing input and choosing one name.
    Native execution has been retaining transfer, source fencing and cancellation.
    Callable entries have been creating fresh targets from ToolContext and routing
    input after selection. These targets have been inheriting the current model,
    provider, output contract and runtime grants, with ordinary Python tools.
    Runtime-created targets have not been supporting durable storage or portable
    checkpoints; concrete captured targets have been retaining those existing paths.
    """

    name: str
    description: str
    question: str
    targets: Mapping[
        str,
        Agent[Any, Any]
        | Callable[
            [ToolContext[Any], str], Agent[Any, Any] | Awaitable[Agent[Any, Any]]
        ],
    ]

    def __post_init__(self) -> None:
        from .agent import Agent

        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.name) is None
        ):
            raise ValueError(
                "Decision handoff name must contain 1 to 64 letters, digits, '_' or '-'"
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Decision handoff description must be nonempty")
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("Decision handoff question must be nonempty")
        if (
            not isinstance(self.targets, Mapping)
            or not self.targets
            or any(
                not isinstance(name, str)
                or not name.strip()
                or not (isinstance(target, Agent) or callable(target))
                for name, target in self.targets.items()
            )
        ):
            raise ValueError(
                "Decision handoff targets must map nonempty names to "
                "native Agents or application factories"
            )
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))


class _RoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(max_length=8192)


@dataclass(frozen=True)
class _DecisionHandoffSelection:
    target_profile_id: str | None = None
    factory: str | None = None
    target: Agent[Any, Any] | None = None


@dataclass(frozen=True)
class _CapturedDecisionHandoff(_CapturedFunction):
    pass


def _decision_handoff_tool(
    declaration: DecisionHandoff, targets: Mapping[str, Any]
) -> FunctionTool[Any, Any, Any]:
    captured_targets = MappingProxyType(dict(targets))

    def bind(
        context: Any, policy_context: Any, tool: FunctionTool[Any, Any, Any]
    ) -> _CapturedFunction:
        async def invoke(
            tool_context: ToolContext[Any], input: str
        ) -> _DecisionHandoffSelection:
            if len(input.encode("utf-8")) > 8192:
                raise ValueError("Decision handoff input exceeds 8192 UTF-8 bytes")
            selected = await tool_context.decide(
                declaration.question, input=input, choices=tuple(captured_targets)
            )
            if not isinstance(selected, str) or selected not in captured_targets:
                raise ValueError("Decision handoff selected an unauthorized target")
            target = captured_targets[selected]
            if callable(target):
                value = target(tool_context, input)
                if inspect.isawaitable(value):
                    value = await value
                from .agent import Agent

                if not isinstance(value, Agent):
                    raise TypeError(
                        "Decision handoff factory must return a native Agent"
                    )
                return _DecisionHandoffSelection(factory=selected, target=value)
            return _DecisionHandoffSelection(target)

        return _CapturedDecisionHandoff(
            name=declaration.name,
            spec_json=tool._spec_json(),
            model=_RoutingInput,
            field_names=("input",),
            output_adapter=TypeAdapter(type(None)),
            callback=invoke,
            context_parameter=inspect.Parameter(
                "tool_context", inspect.Parameter.POSITIONAL_ONLY
            ),
            callback_timeout=None,
            timeout_behavior="raise_exception",
            timeout_error_function=None,
            before=None,
            after=None,
            context=context,
        )

    return FunctionTool(
        name=declaration.name,
        description=declaration.description,
        input_type=_RoutingInput,
        output_type=type(None),
        _bind=bind,
    )


_preserve_module_identity(globals(), "agents.decision_handoff")
