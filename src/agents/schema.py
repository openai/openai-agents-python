"""Closed output declarations and their captured native or prompted schemas."""

from __future__ import annotations

from ._runtime.schema import (
    Any as Any,
    ConfigDict as ConfigDict,
    Generic as Generic,
    Literal as Literal,
    NativeOutput as NativeOutput,
    OutputSpec as OutputSpec,
    OutputT as OutputT,
    OutputValidationError as OutputValidationError,
    PromptedOutput as PromptedOutput,
    TypeAdapter as TypeAdapter,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    ValidationError as ValidationError,
    _prepare_output as _prepare_output,
    _prepare_spec as _prepare_spec,
    _PreparedOutput as _PreparedOutput,
    create_model as create_model,
    dataclass as dataclass,
    hashlib as hashlib,
    json as json,
    output_schema as output_schema,
)
