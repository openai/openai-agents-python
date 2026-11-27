"""Private, per-invocation preparation and conservative approval inspection."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MethodType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasPath, BaseModel
from pydantic_core import PydanticUndefined, SchemaValidator, ValidationError, core_schema

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


_STRICT_STRINGS = {
    "date": SchemaValidator(core_schema.date_schema(strict=True)),
    "uuid": SchemaValidator(core_schema.uuid_schema(strict=True)),
}


def _direct_input_key(
    name: str, alias: Any, config: dict[str, Any], values: dict[str, Any]
) -> str | None:
    if alias is None or not config.get("validate_by_alias", True):
        return name
    if isinstance(alias, str):
        if alias in values:
            return alias
    else:
        # A validation alias can be a single path or ordered choices of paths.
        paths = alias if isinstance(alias[0], list) else [alias]
        for path in paths:
            if AliasPath(*path).search_dict_for_path(values) is not PydanticUndefined:
                # A nested match owns the field. Leave shared inputs alone.
                return path[0] if len(path) == 1 else None
    return name if config.get("validate_by_name") else None


def _parameter_fields(schema: Any, model: type[BaseModel]) -> Any:
    inner = schema["schema"]
    if inner["type"] == "function-before":
        callback = inner["function"]["function"]
        # Only the generated tool model's own decoder is safe to unwrap.
        if (
            type(callback) is MethodType
            and callback.__self__ is model
            and callback.__func__ is prepare_strict_json_values
        ):
            return inner["schema"]
    return inner


def prepare_strict_json_values(cls: type[BaseModel], arguments: Any) -> Any:
    """Decode strict wire values after plugin entry and before field validation."""
    schema = cls.__pydantic_core_schema__
    definitions = {item["ref"]: item for item in schema.get("definitions", ())}

    def prepare(node: Any, value: Any, strict: bool = False) -> Any:
        config = {}
        # Schema wrappers can be several layers deep per recursive model.
        # Recurse only when descending into a JSON container.
        while True:
            kind = node["type"]
            if kind == "definition-ref":
                node = definitions[node["schema_ref"]]
            elif kind == "model" and not node.get("custom_init"):
                config = node.get("config", {})
                strict = config.get("strict", False)
                node = _parameter_fields(node, cls)
            elif kind == "default" and node.get("on_error") in ("omit", "default"):
                # Python validation already accepts these inputs via the explicit fallback.
                return value
            elif kind in ("definitions", "nullable", "default", "function-after"):
                node = node["schema"]
            else:
                break
        if kind == "model-fields" and isinstance(value, dict):
            for name, field in node["fields"].items():
                key = _direct_input_key(name, field.get("validation_alias"), config, value)
                if key is not None and key in value:
                    value[key] = prepare(field["schema"], value[key], strict)
        elif kind == "list" and isinstance(value, list) and "items_schema" in node:
            for i, item in enumerate(value):
                value[i] = prepare(node["items_schema"], item, strict)
        elif kind == "dict" and isinstance(value, dict) and "values_schema" in node:
            for key, item in value.items():
                value[key] = prepare(node["values_schema"], item, strict)
        elif kind == "tuple" and isinstance(value, list):
            items = node["items_schema"]
            for i, item in enumerate(value):
                if len(items) == 1 and node.get("variadic_item_index") == 0:
                    value[i] = prepare(items[0], item, strict)
                elif "variadic_item_index" not in node and i < len(items):
                    value[i] = prepare(items[i], item, strict)
            if node.get("strict", strict):
                return tuple(value)
        elif kind in _STRICT_STRINGS and node.get("strict", strict) and isinstance(value, str):
            try:
                return _STRICT_STRINGS[kind].validate_json(json.dumps(value))
            except ValidationError:
                pass  # The original model reports the error with its field path.
        # Leave branch selection and raw input for before/wrap validators to Pydantic.
        return value

    return prepare(schema, arguments)


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
            and _is_data_only_schema(_parameter_fields(schema, model))
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
