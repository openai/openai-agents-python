from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class ModelSettings:
    """Settings to use when calling an LLM.

    This class holds optional model configuration parameters (e.g. temperature,
    top_p, penalties, truncation, etc.).
    """

    temperature: float | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    tool_choice: Literal["auto", "required", "none"] | str | None = None
    parallel_tool_calls: bool | None = False
    truncation: Literal["auto", "disabled"] | None = None

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
        )
