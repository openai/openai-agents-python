from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from openai.types.responses.response_prompt_param import (
    ResponsePromptParam,
    Variables as ResponsesPromptVariables,
)
from typing_extensions import NotRequired, TypedDict

from agents.util._types import MaybeAwaitable

from .exceptions import UserError
from .run_context import RunContextWrapper

if TYPE_CHECKING:
    from .agent import Agent


class Prompt(TypedDict):
    """Prompt configuration to use for interacting with an OpenAI model."""

    id: str
    """The unique ID of the prompt."""

    version: NotRequired[str]
    """Optional version of the prompt."""

    variables: NotRequired[dict[str, ResponsesPromptVariables]]
    """Optional variables to substitute into the prompt."""


@dataclass
class GenerateDynamicPromptData:
    """Inputs to a function that allows you to dynamically generate a prompt."""

    context: RunContextWrapper[Any]
    """The run context."""

    agent: Agent[Any]
    """The agent for which the prompt is being generated."""


DynamicPromptFunction = Callable[[GenerateDynamicPromptData], MaybeAwaitable[Prompt]]
"""A function that dynamically generates a prompt."""


def _coerce_prompt_dict(prompt: Prompt | dict[object, object]) -> Prompt:
    """Convert a runtime-validated prompt dict into the Prompt TypedDict view."""
    return cast(Prompt, prompt)


def validate_prompt(prompt: object, *, source: str = "Prompt") -> Prompt:
    """Validate a prompt config before forwarding it to a model provider."""
    if not isinstance(prompt, dict):
        raise UserError(f"{source} must be a Prompt")

    prompt_id = prompt.get("id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise UserError(f"{source} must include a non-empty string 'id'")

    version = prompt.get("version")
    if version is not None and not isinstance(version, str):
        raise UserError(f"{source} 'version' must be a string when provided")

    variables = prompt.get("variables")
    if variables is not None and not isinstance(variables, dict):
        raise UserError(f"{source} 'variables' must be a dict when provided")

    return _coerce_prompt_dict(prompt)


class PromptUtil:
    @staticmethod
    async def to_model_input(
        prompt: Prompt | DynamicPromptFunction | None,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
    ) -> ResponsePromptParam | None:
        if prompt is None:
            return None

        resolved_prompt: Prompt
        if isinstance(prompt, dict):
            resolved_prompt = validate_prompt(prompt)
        else:
            if not callable(prompt):
                raise UserError("Agent prompt must be a Prompt or DynamicPromptFunction")
            func_result = prompt(GenerateDynamicPromptData(context=context, agent=agent))
            if inspect.isawaitable(func_result):
                resolved_prompt = await func_result
            else:
                resolved_prompt = func_result
            resolved_prompt = validate_prompt(
                resolved_prompt,
                source="Dynamic prompt function return value",
            )

        return {
            "id": resolved_prompt["id"],
            "version": resolved_prompt.get("version"),
            "variables": resolved_prompt.get("variables"),
        }
