"""Closed output declarations and their captured native or prompted schemas."""

from __future__ import annotations

import hashlib as hashlib
import json as json
from dataclasses import dataclass as dataclass
from typing import (
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
)

from pydantic import (
    ConfigDict as ConfigDict,
    TypeAdapter as TypeAdapter,
    ValidationError as ValidationError,
    create_model as create_model,
)

from ._public import preserve_module_identity as _preserve_module_identity

OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class NativeOutput(Generic[OutputT]):
    """Request a native strict schema and validate the final output once in Python.

    An explicit NativeOutput(str) uses the native response object, not plain
    text. Unsupported native schema shapes fail before a turn starts.
    """

    type_: type[OutputT]


@dataclass(frozen=True)
class PromptedOutput(Generic[OutputT]):
    """Request JSON through the turn's prompt, validated in Python.

    Core generates ordinary text, with no native schema guarantee. By default
    validation failure raises immediately. max_retries opts into up to N
    separate, tool-free native correction attempts for invalid final text.
    The task and its tools are never replayed.

    The correction gets the invalid text, schema and the effective model
    connection, but not the original tools, context or instructions. Repair
    receipts are separate from original thread usage and native model history.
    LocalStorage does not support nonzero max_retries. Portable capture is
    rejected while repair is active. After repair attempts settle, including
    exhausted retries, the SDK preserves complete receipts at otherwise
    supported portable capture boundaries.
    """

    type_: type[OutputT]
    max_retries: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise TypeError("max_retries must be a nonnegative integer")
        if self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")


OutputSpec: TypeAlias = type[OutputT] | NativeOutput[OutputT] | PromptedOutput[OutputT]


@dataclass(frozen=True)
class OutputValidationIssue:
    """Sensitive diagnostic selected from one failed output validation.

    ``path`` addresses application output. Native non-object roots omit the
    SDK's ``response`` prefix; failures in that wrapper use the root path ().
    Codes and paths may contain application data. Access them deliberately,
    with the same care as ``OutputValidationError.raw_output``.
    """

    code: str
    path: tuple[str | int, ...]


class OutputValidationError(ValueError):
    """The final text failed its captured output contract.

    raw_output and issues are deliberate application access and may contain
    sensitive data, including application-defined codes and field names.
    The exception message does not contain provider output or validation inputs.
    """

    def __init__(self, raw_output: str, issues: tuple[OutputValidationIssue, ...] = ()) -> None:
        super().__init__("Final output did not match the requested output type")
        self.raw_output = raw_output
        self.issues = issues


@dataclass(frozen=True)
class _PreparedOutput:
    mode: Literal["plain", "native", "prompted"]
    adapter: TypeAdapter[Any] | None
    schema: dict[str, Any] | None
    wrapped: bool = False
    max_retries: int = 0

    @property
    def fingerprint(self) -> str:
        identity: dict[str, Any] = {"mode": self.mode, "schema": self.schema}
        # Keep existing zero-retry checkpoints compatible.
        if self.max_retries:
            identity["max_retries"] = self.max_retries
        value = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def validate(self, text: str) -> Any:
        return self._validate(text, terminal=False)[0]

    def terminal_value(self, text: str) -> tuple[Any, Any]:
        """Validate once and serialize the same validated value for Core."""
        return self._validate(text, terminal=True)

    def _validate(self, text: str, *, terminal: bool) -> tuple[Any, Any]:
        if self.adapter is None:
            return text, text
        try:
            # The function supplies application JSON, without the native response wrapper.
            source = (
                json.dumps({"response": json.loads(text)}) if terminal and self.wrapped else text
            )
            decoded = self.adapter.validate_json(source)
            value = decoded.response if self.wrapped else decoded
            wire = None
            if terminal:
                wire = json.loads(self.adapter.dump_json(decoded, warnings="error"))
                if self.wrapped:
                    wire = wire["response"]
            return value, wire
        except ValidationError as error:
            issues = tuple(
                OutputValidationIssue(
                    code=issue["type"],
                    path=(issue["loc"][1:] if issue["loc"][:1] == ("response",) else ())
                    if self.wrapped
                    else issue["loc"],
                )
                for issue in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            )
        except (TypeError, ValueError):
            issues = (OutputValidationIssue(code="validation_error", path=()),)
        # Do not attach the validator exception, which contains rejected inputs.
        raise OutputValidationError(text, issues)


def _prepare_spec(output: OutputSpec[Any] | None) -> _PreparedOutput:
    if output is None or output is str:
        return _PreparedOutput("plain", None, None)
    if isinstance(output, PromptedOutput):
        adapter: TypeAdapter[Any] = TypeAdapter(output.type_)
        return _PreparedOutput(
            "prompted", adapter, adapter.json_schema(), max_retries=output.max_retries
        )
    native_type = output.type_ if isinstance(output, NativeOutput) else output
    adapter, schema, wrapped = _prepare_output(native_type)
    return _PreparedOutput("native", adapter, schema, wrapped)


def output_schema(output_type: Any) -> dict[str, Any] | None:
    return _prepare_output(output_type)[1]


def _prepare_output(output_type: Any) -> tuple[TypeAdapter[Any], dict[str, Any], bool]:
    adapter: TypeAdapter[Any] = TypeAdapter(output_type)
    schema = adapter.json_schema()
    if "$ref" in schema:
        reference = schema.pop("$ref")
        if not reference.startswith("#/$defs/"):
            raise ValueError("output_type requires a local object schema")
        definition = schema["$defs"][reference.removeprefix("#/$defs/")]
        schema = {**definition, **schema}
    wrapped = schema.get("type") != "object"
    if wrapped:
        adapter = TypeAdapter(
            create_model(
                "_AgentOutput",
                __config__=ConfigDict(extra="forbid"),
                response=(output_type, ...),
            )
        )
        schema = adapter.json_schema()

    def close(node: dict[str, Any]) -> None:
        if not any(key in node for key in ("type", "$ref", "anyOf")):
            raise ValueError("output_type cannot contain unconstrained values")
        if any(key in node for key in ("oneOf", "allOf", "prefixItems", "uniqueItems")):
            raise ValueError("output_type contains an unsupported strict schema shape")
        node.pop("default", None)
        if node.get("type") == "object":
            if node.get("additionalProperties", False) is not False:
                raise ValueError("output_type cannot contain open maps or extra fields")
            properties = node.setdefault("properties", {})
            node["additionalProperties"] = False
            node["required"] = list(properties)
            for child in properties.values():
                close(child)
        for definition in node.get("$defs", {}).values():
            close(definition)
        if "items" in node:
            close(node["items"])
        for branch in node.get("anyOf", []):
            close(branch)

    close(schema)
    return adapter, schema, wrapped


_preserve_module_identity(globals(), "agents.schema")
