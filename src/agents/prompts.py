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


def _validate_prompt_dict(prompt: Prompt | dict[object, object]) -> Prompt:
    """Validate and convert a prompt dict into the Prompt TypedDict view."""
    prompt_id = prompt.get("id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise UserError("Prompt config must include a non-empty string 'id'")

    version = prompt.get("version")
    if version is not None and not isinstance(version, str):
        raise UserError("Prompt config 'version' must be a string when provided")

    variables = prompt.get("variables")
    if variables is not None and not isinstance(variables, dict):
        raise UserError("Prompt config 'variables' must be a dict when provided")

    return cast(Prompt, prompt)


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
            resolved_prompt = PromptUtil.validate_prompt_config(prompt)
        else:
            func_result = prompt(GenerateDynamicPromptData(context=context, agent=agent))
            if inspect.isawaitable(func_result):
                resolved_prompt = await func_result
            else:
                resolved_prompt = func_result
            if not isinstance(resolved_prompt, dict):
                raise UserError("Dynamic prompt function must return a Prompt")
            resolved_prompt = PromptUtil.validate_prompt_config(resolved_prompt)

        return {
            "id": resolved_prompt["id"],
            "version": resolved_prompt.get("version"),
            "variables": resolved_prompt.get("variables"),
        }

    @staticmethod
    def validate_prompt_config(prompt: Prompt | dict[object, object]) -> Prompt:
        return _validate_prompt_dict(prompt)
