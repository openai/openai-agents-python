from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

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
            resolved_prompt = prompt
        else:
            func_result = prompt(GenerateDynamicPromptData(context=context, agent=agent))
            if inspect.isawaitable(func_result):
                resolved_prompt = await func_result
            else:
                resolved_prompt = func_result
            if not isinstance(resolved_prompt, dict):
                raise UserError("Dynamic prompt function must return a Prompt")

        return {
            "id": resolved_prompt["id"],
            "version": resolved_prompt.get("version"),
            "variables": resolved_prompt.get("variables"),
        }
