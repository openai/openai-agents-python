from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class ModelSettings:
    """Settings to use when calling an LLM.

    This class holds optional model configuration parameters (e.g. temperature,
    top_p, penalties, truncation, etc.).

    Not all models/providers support all of these parameters, so please check the API documentation
    for the specific model and provider you are using.
    """

    temperature: float | None = None
    """The temperature to use when calling the model."""

    top_p: float | None = None
    """The top_p to use when calling the model."""

    frequency_penalty: float | None = None
    """The frequency penalty to use when calling the model."""

    presence_penalty: float | None = None
    """The presence penalty to use when calling the model."""

    tool_choice: Literal["auto", "required", "none"] | str | None = None
    """The tool choice to use when calling the model."""

    parallel_tool_calls: bool | None = None
    """Whether to use parallel tool calls when calling the model."""

    truncation: Literal["auto", "disabled"] | None = None
    """The truncation strategy to use when calling the model."""

    max_tokens: int | None = None
    """The maximum number of output tokens to generate."""

    store: bool | None = None
    """Whether to store the generated model response for later retrieval."""

    def resolve(self, override: ModelSettings | None) -> ModelSettings:
        """Produce a new ModelSettings by overlaying any non-None values from the
        override on top of this instance."""
        if override is None:
            return self
        return ModelSettings(
            temperature=override.temperature if override.temperature is not None else self.temperature,
            top_p=override.top_p if override.top_p is not None else self.top_p,
            frequency_penalty=override.frequency_penalty if override.frequency_penalty is not None else self.frequency_penalty,
            presence_penalty=override.presence_penalty if override.presence_penalty is not None else self.presence_penalty,
            tool_choice=override.tool_choice if override.tool_choice is not None else self.tool_choice,
            parallel_tool_calls=override.parallel_tool_calls if override.parallel_tool_calls is not None else self.parallel_tool_calls,
            truncation=override.truncation if override.truncation is not None else self.truncation,
            max_tokens=override.max_tokens if override.max_tokens is not None else self.max_tokens,
            store=override.store if override.store is not None else self.store,
        )
