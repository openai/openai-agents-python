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

    parallel_tool_calls: bool | None = False
    """Whether to use parallel tool calls when calling the model."""

    truncation: Literal["auto", "disabled"] | None = None
    """The truncation strategy to use when calling the model."""

    max_tokens: int | None = None
    """The maximum number of output tokens to generate."""

    def resolve(self, override: ModelSettings | None) -> ModelSettings:
        """Produce a new ModelSettings by overlaying any non-None values from the
        override on top of this instance."""
        if override is None:
            return self
        return ModelSettings(
            temperature=override.temperature or self.temperature,
            top_p=override.top_p or self.top_p,
            frequency_penalty=override.frequency_penalty or self.frequency_penalty,
            presence_penalty=override.presence_penalty or self.presence_penalty,
            tool_choice=override.tool_choice or self.tool_choice,
            parallel_tool_calls=override.parallel_tool_calls or self.parallel_tool_calls,
            truncation=override.truncation or self.truncation,
            max_tokens=override.max_tokens or self.max_tokens,
        )
