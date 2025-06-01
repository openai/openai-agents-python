from __future__ import annotations

import dataclasses
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from openai._types import Body, Headers, Query
from openai.types.shared import Reasoning
from pydantic import BaseModel


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
    """Controls whether the model can make multiple parallel tool calls in a single turn.
    If not provided (i.e., set to None), this behavior defers to the underlying
    model provider's default. For most current providers (e.g., OpenAI), this typically
    means parallel tool calls are enabled (True).
    Set to True to explicitly enable parallel tool calls, or False to restrict the
    model to at most one tool call per turn.
    """

    truncation: Literal["auto", "disabled"] | None = None
    """The truncation strategy to use when calling the model."""

    max_tokens: int | None = None
    """The maximum number of output tokens to generate."""

    reasoning: Reasoning | None = None
    """Configuration options for
    [reasoning models](https://platform.openai.com/docs/guides/reasoning).
    """

    metadata: dict[str, str] | None = None
    """Metadata to include with the model response call."""

    store: bool | None = None
    """Whether to store the generated model response for later retrieval.
    Defaults to True if not provided."""

    include_usage: bool | None = None
    """Whether to include usage chunk.
    Defaults to True if not provided."""

    extra_query: Query | None = None
    """Additional query fields to provide with the request.
    Defaults to None if not provided."""

    extra_body: Body | None = None
    """Additional body fields to provide with the request.
    Defaults to None if not provided."""

    extra_headers: Headers | None = None
    """Additional headers to provide with the request.
    Defaults to None if not provided."""

    def resolve(self, override: ModelSettings | None) -> ModelSettings:
        """Produce a new ModelSettings by overlaying any non-None values from the
        override on top of this instance."""
        if override is None:
            return self

        changes = {
            field.name: getattr(override, field.name)
            for field in fields(self)
            if getattr(override, field.name) is not None
        }
        return replace(self, **changes)

    def to_json_dict(self) -> dict[str, Any]:
        dataclass_dict = dataclasses.asdict(self)

        json_dict: dict[str, Any] = {}

        for field_name, value in dataclass_dict.items():
            if isinstance(value, BaseModel):
                json_dict[field_name] = value.model_dump(mode="json")
            else:
                json_dict[field_name] = value

        return json_dict
