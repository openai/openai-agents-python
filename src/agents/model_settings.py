from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Annotated, Any, Literal, Union

from openai import Omit as _Omit
from openai._types import Body, Query
from openai.types.responses import ResponseIncludable
from openai.types.shared import Reasoning
from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import core_schema
from typing_extensions import TypeAlias


class _OmitTypeAnnotation:
    @classmethod
    def __get_pydantic_core_schema__(
            cls,
            _source_type: Any,
            _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        def validate_from_none(value: None) -> _Omit:
            return _Omit()

        from_none_schema = core_schema.chain_schema(
            [
                core_schema.none_schema(),
                core_schema.no_info_plain_validator_function(validate_from_none),
            ]
        )
        return core_schema.json_or_python_schema(
            json_schema=from_none_schema,
            python_schema=core_schema.union_schema(
                [
                    # check if it's an instance first before doing any further work
                    core_schema.is_instance_schema(_Omit),
                    from_none_schema,
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda instance: None
            ),
        )
Omit = Annotated[_Omit, _OmitTypeAnnotation]
Headers: TypeAlias = Mapping[str, Union[str, Omit]]

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

    response_include: list[ResponseIncludable] | None = None
    """Additional output data to include in the model response.
    [include parameter](https://platform.openai.com/docs/api-reference/responses/create#responses-create-include)"""

    extra_query: Query | None = None
    """Additional query fields to provide with the request.
    Defaults to None if not provided."""

    extra_body: Body | None = None
    """Additional body fields to provide with the request.
    Defaults to None if not provided."""

    extra_headers: Headers | None = None
    """Additional headers to provide with the request.
    Defaults to None if not provided."""

    extra_args: dict[str, Any] | None = None
    """Arbitrary keyword arguments to pass to the model API call.
    These will be passed directly to the underlying model provider's API.
    Use with caution as not all models support all parameters."""

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

        # Handle extra_args merging specially - merge dictionaries instead of replacing
        if self.extra_args is not None or override.extra_args is not None:
            merged_args = {}
            if self.extra_args:
                merged_args.update(self.extra_args)
            if override.extra_args:
                merged_args.update(override.extra_args)
            changes["extra_args"] = merged_args if merged_args else None

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
