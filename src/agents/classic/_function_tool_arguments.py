"""Private, per-invocation preparation and conservative approval inspection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel
from pydantic_core import SchemaValidator

from .exceptions import UserError

if TYPE_CHECKING:
    from .tool import FunctionTool


@dataclass
class PreparedFunctionArguments:
    owner: object
    arguments: str
    args: list[Any]
    kwargs: dict[str, Any]
    unchanged: bool = False


@dataclass
class FunctionToolApproval:
    outcome: Literal["invoke", "require_approval"]
    tool: FunctionTool
    invoker: object
    arguments: str
    prepared: PreparedFunctionArguments | None = None

    def check_invocation(self, tool: FunctionTool, arguments: str) -> None:
        if (
            self.tool is not tool
            or self.invoker is not tool.on_invoke_tool
            or self.arguments != arguments
        ):
            raise UserError("Function tool invocation changed during approval evaluation.")


class _UninspectableArguments(Exception):
    pass


def can_prepare_without_user_code(model: type[BaseModel]) -> bool:
    """Allow only data-only validation of the SDK-generated parameter model.

    Application models, validation callbacks, plugins, and custom default factories
    require explicit approval. Inspect the validator and core schema; never execute
    validation to discover whether it has effects. Unknown schema types also require approval.
    """
    # Pydantic plugin hooks wrap the validator outside its core schema.
    if type(model.__pydantic_validator__) is not SchemaValidator:
        return False
    schema = model.__pydantic_core_schema__
    try:
        return (
            schema.get("type") == "model"
            and schema.get("cls") is model
            and not schema.get("custom_init")
            and not schema.get("post_init")
            and _is_data_only_schema(schema["schema"])
        )
    except RecursionError:
        return False


def _is_data_only_schema(schema: Any) -> bool:
    kind = schema["type"]
    if kind in ("any", "none", "bool", "int", "float"):
        return True
    if kind == "str":
        # Pattern constraints can invoke Python's backtracking regex engine.
        # Leave matching to the ordinary validation path after approval.
        return "pattern" not in schema
    if kind == "model-fields":
        return not schema.get("extras_schema") and all(
            _is_data_only_schema(field["schema"]) for field in schema["fields"].values()
        )
    if kind == "list":
        return _is_data_only_schema(schema.get("items_schema", {"type": "any"}))
    if kind == "dict":
        return all(
            _is_data_only_schema(schema.get(key, {"type": "any"}))
            for key in ("keys_schema", "values_schema")
        )
    if kind == "nullable":
        return _is_data_only_schema(schema["schema"])
    if kind == "union":
        return all(
            type(choice) is dict and _is_data_only_schema(choice) for choice in schema["choices"]
        )
    if kind == "literal":
        return all(value is None or type(value) in (bool, int, str) for value in schema["expected"])
    if kind == "default":
        factory = schema.get("default_factory")
        # The schema generator uses these built-ins for *args and **kwargs.
        if factory is not None and factory is not list and factory is not dict:
            return False
        try:
            _project_json(schema.get("default"), set())
        except (_UninspectableArguments, RecursionError):
            return False
        return _is_data_only_schema(schema["schema"])
    return False


def arguments_unchanged(parsed: BaseModel, raw: dict[str, Any]) -> bool:
    """Compare execution fields without running serializers or custom equality.

    Only the generated root parameter-binding object ignores key order. Every
    dictionary value preserves execution order. Application models are excluded
    before validation and are never projected here.
    """
    try:
        fields = type(parsed).model_fields
        storage = object.__getattribute__(parsed, "__dict__")
        projected: dict[str, Any] = {}
        for name, value in storage.items():
            field = fields[name]
            alias = field.validation_alias if field.validation_alias is not None else field.alias
            if alias is not None and type(alias) is not str:
                return False
            key = alias or name
            if key in projected:
                return False
            projected[key] = _project_json(value, set())
        return _same_json(projected, raw, root=True)
    except (_UninspectableArguments, RecursionError):
        return False


def _project_json(value: Any, active: set[int]) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _UninspectableArguments
        return value
    if id(value) in active:
        raise _UninspectableArguments
    active.add(id(value))
    try:
        if type(value) is list:
            return [_project_json(item, active) for item in value]
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise _UninspectableArguments
            return {key: _project_json(item, active) for key, item in value.items()}
        raise _UninspectableArguments
    finally:
        active.remove(id(value))


def _same_json(left: Any, right: Any, *, root: bool = False) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if (left.keys() != right.keys()) if root else (list(left) != list(right)):
            return False
        return all(_same_json(value, right[key]) for key, value in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=False)
        )
    if type(left) is float:
        return bool(left.hex() == right.hex())
    return bool(left == right)
