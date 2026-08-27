from __future__ import annotations

import copy
from typing import Any, TypeGuard, cast

from openai import NOT_GIVEN

from .exceptions import UserError

_EMPTY_SCHEMA = {
    "additionalProperties": False,
    "type": "object",
    "properties": {},
    "required": [],
}

# Upper bound on how many schema nodes strict conversion will expand. Real schemas are far
# smaller; the limit only trips on pathological input such as a `$ref` fan-out that would
# otherwise expand exponentially -- a denial-of-service vector for untrusted schemas (for
# example, tool schemas advertised by a third-party MCP server).
_MAX_SCHEMA_NODES = 100_000

# Keep recursive copying and normalization comfortably below Python's recursion limit. This
# counts every nested dictionary or list, including schema maps such as `properties` and `$defs`.
_MAX_SCHEMA_DEPTH = 100

_ADDITIONAL_PROPERTIES_ERROR = (
    "additionalProperties should not be set for object types. This could be because "
    "you're using an older version of Pydantic, or because you configured additional "
    "properties to be allowed. If you really need this, update the function or output tool "
    "to not use a strict schema."
)

_OPEN_OBJECT_ERROR = (
    "JSON schema contains an object that permits undeclared properties and cannot be converted "
    "to a strict schema without changing its accepted values."
)

_UNVALIDATED_REF_ERROR = (
    "JSON schema contains a reference whose target was not validated for strict mode."
)
_NESTED_RESOURCE_REF_ERROR = (
    "JSON schema contains a reference owned by or crossing a nested `$id` resource that cannot "
    "be resolved against the document root."
)

_SCHEMA_DEPTH_ERROR = (
    "JSON schema is too deeply nested to process safely. Simplify or flatten the schema."
)


def _validate_json_schema_depth(schema: object) -> None:
    """Reject container nesting that is unsafe for recursive schema processing."""
    stack: list[tuple[object, int]] = [(schema, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > _MAX_SCHEMA_DEPTH:
            raise UserError(_SCHEMA_DEPTH_ERROR)

        if isinstance(value, dict):
            stack.extend(
                (child, depth + 1) for child in value.values() if isinstance(child, dict | list)
            )
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value if isinstance(child, dict | list))


def _copy_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy a JSON schema only after verifying recursive copying is safe."""
    _validate_json_schema_depth(schema)
    return copy.deepcopy(schema)


# Keywords that may legally sit alongside a `$ref` without adding validation constraints.
# Definition maps are also allowed because they do not directly constrain an instance.
_REF_NON_CONSTRAINING_SIBLINGS = frozenset(
    {
        "$anchor",
        "$comment",
        "$defs",
        "$schema",
        "contentEncoding",
        "contentMediaType",
        "contentSchema",
        "default",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_ROOT_REF_NON_CONSTRAINING_SIBLINGS = _REF_NON_CONSTRAINING_SIBLINGS | {"$id"}


class _NodeBudget:
    """Tracks conversion state across the recursion."""

    def __init__(self, limit: int, *, reject_open_objects: bool = False) -> None:
        self.remaining = limit
        self.reject_open_objects = reject_open_objects

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise UserError(
                "JSON schema is too large to convert to a strict schema. This can happen when a "
                "schema expands `$ref`s exponentially, which may indicate a malformed or malicious "
                "schema."
            )


def ensure_strict_json_schema(
    schema: dict[str, Any],
    *,
    _reject_open_objects: bool = False,
) -> dict[str, Any]:
    """Mutates the given JSON schema to ensure it conforms to the `strict` standard
    that the OpenAI API expects.
    """
    _validate_json_schema_depth(schema)
    if schema == {}:
        return copy.deepcopy(_EMPTY_SCHEMA)
    budget = _NodeBudget(_MAX_SCHEMA_NODES, reject_open_objects=_reject_open_objects)
    return _ensure_strict_root(
        _ensure_strict_json_schema(
            schema,
            path=(),
            root=schema,
            budget=budget,
        )
    )


def _ensure_strict_root(schema: dict[str, Any]) -> dict[str, Any]:
    if is_list(schema.get("anyOf")):
        raise UserError("The root of a strict JSON schema must not use `anyOf`.")

    typ = schema.get("type")
    if is_list(typ) and "object" in typ:
        if typ == ["object"]:
            schema["type"] = "object"
        else:
            raise UserError(
                "The root of a strict JSON schema must be a non-nullable object, but its type is "
                f"{typ}. Make the root a plain object, or update the function or output tool to "
                "not use a strict schema."
            )
    return schema


# Adapted from https://github.com/openai/openai-python/blob/main/src/openai/lib/_pydantic.py
def _ensure_strict_json_schema(
    json_schema: object,
    *,
    path: tuple[str, ...],
    root: dict[str, object],
    budget: _NodeBudget | None = None,
    depth: int = 1,
    inside_nested_resource: bool = False,
) -> dict[str, Any]:
    if depth > _MAX_SCHEMA_DEPTH:
        raise UserError(_SCHEMA_DEPTH_ERROR)

    if not is_dict(json_schema):
        raise TypeError(f"Expected {json_schema} to be a dictionary; path={path}")

    # Bound the total number of nodes we expand so a malicious `$ref` fan-out cannot expand
    # exponentially and exhaust CPU and memory.
    if budget is None:
        budget = _NodeBudget(_MAX_SCHEMA_NODES)
    budget.spend()
    next_depth = depth + 1
    inside_nested_resource = inside_nested_resource or (
        json_schema is not root and _declares_schema_resource(json_schema)
    )

    if "$ref" in json_schema:
        if inside_nested_resource:
            raise UserError(_NESTED_RESOURCE_REF_ERROR)
        allowed_siblings = (
            _ROOT_REF_NON_CONSTRAINING_SIBLINGS
            if json_schema is root
            else _REF_NON_CONSTRAINING_SIBLINGS
        )
        incompatible_siblings = set(json_schema) - {"$ref"} - allowed_siblings
        if incompatible_siblings:
            raise UserError(
                "JSON schema contains a `$ref` with incompatible sibling keyword(s) "
                f"({', '.join(sorted(incompatible_siblings))}) that cannot be merged "
                "without changing its accepted values."
            )

    defs = json_schema.get("$defs")
    if is_dict(defs):
        for def_name, def_schema in defs.items():
            _ensure_strict_json_schema(
                def_schema,
                path=(*path, "$defs", def_name),
                root=root,
                budget=budget,
                depth=next_depth,
                inside_nested_resource=inside_nested_resource,
            )

    definitions = json_schema.get("definitions")
    if is_dict(definitions):
        for definition_name, definition_schema in definitions.items():
            _ensure_strict_json_schema(
                definition_schema,
                path=(*path, "definitions", definition_name),
                root=root,
                budget=budget,
                depth=next_depth,
                inside_nested_resource=inside_nested_resource,
            )

    typ = json_schema.get("type")
    properties = json_schema.get("properties")
    if typ is None and is_dict(properties):
        typ = json_schema["type"] = "object"
    elif typ is None and json_schema.get("additionalProperties", False) is not False:
        raise UserError(_ADDITIONAL_PROPERTIES_ERROR)
    is_object = typ == "object" or (is_list(typ) and "object" in typ)
    has_no_declared_properties = "properties" not in json_schema or properties == {}
    if (
        budget.reject_open_objects
        and is_object
        and has_no_declared_properties
        and json_schema.get("additionalProperties") is not False
    ):
        raise UserError(_OPEN_OBJECT_ERROR)
    if is_object and "additionalProperties" not in json_schema:
        json_schema["additionalProperties"] = False
    elif (
        is_object
        and "additionalProperties" in json_schema
        # Compare with ``is not False`` rather than truthiness: OpenAPI/MCP schemas often use
        # ``additionalProperties: {}`` (an empty schema meaning "allow anything"). That value is
        # falsy in Python, so a truthiness check would silently leave a non-strict schema in place.
        and json_schema["additionalProperties"] is not False
    ):
        raise UserError(_ADDITIONAL_PROPERTIES_ERROR)

    # object types
    # { 'type': 'object', 'properties': { 'a':  {...} } }
    if is_dict(properties):
        json_schema["required"] = list(properties.keys())
        json_schema["properties"] = {
            key: _ensure_strict_json_schema(
                prop_schema,
                path=(*path, "properties", key),
                root=root,
                budget=budget,
                depth=next_depth,
                inside_nested_resource=inside_nested_resource,
            )
            for key, prop_schema in properties.items()
        }

    # arrays
    # { 'type': 'array', 'items': {...} }
    items = json_schema.get("items")
    if is_dict(items):
        json_schema["items"] = _ensure_strict_json_schema(
            items,
            path=(*path, "items"),
            root=root,
            budget=budget,
            depth=next_depth,
            inside_nested_resource=inside_nested_resource,
        )

    # unions
    any_of = json_schema.get("anyOf")
    if is_list(any_of):
        json_schema["anyOf"] = [
            _ensure_strict_json_schema(
                variant,
                path=(*path, "anyOf", str(i)),
                root=root,
                budget=budget,
                depth=next_depth,
                inside_nested_resource=inside_nested_resource,
            )
            for i, variant in enumerate(any_of)
        ]

    # oneOf is not supported by OpenAI's structured outputs in nested contexts,
    # so we convert it to anyOf which provides equivalent functionality for
    # discriminated unions
    one_of = json_schema.get("oneOf")
    if is_list(one_of):
        existing_any_of = json_schema.get("anyOf", [])
        if not is_list(existing_any_of):
            existing_any_of = []
        json_schema["anyOf"] = existing_any_of + [
            _ensure_strict_json_schema(
                variant,
                path=(*path, "oneOf", str(i)),
                root=root,
                budget=budget,
                depth=next_depth,
                inside_nested_resource=inside_nested_resource,
            )
            for i, variant in enumerate(one_of)
        ]
        json_schema.pop("oneOf")

    # intersections
    all_of = json_schema.get("allOf")
    if is_list(all_of):
        if len(all_of) == 1:
            entry = all_of[0]
            if (
                is_dict(entry)
                and "$ref" in entry
                and not (set(entry) - {"$ref"} - _REF_NON_CONSTRAINING_SIBLINGS)
            ):
                if inside_nested_resource:
                    raise UserError(_NESTED_RESOURCE_REF_ERROR)
                budget.spend()
                strict_entry = _resolve_non_constraining_ref_chain(
                    root=root,
                    schema=entry,
                    budget=budget,
                )
            else:
                strict_entry = _ensure_strict_json_schema(
                    entry,
                    path=(*path, "allOf", "0"),
                    root=root,
                    budget=budget,
                    depth=next_depth,
                    inside_nested_resource=inside_nested_resource,
                )
            json_schema.pop("allOf")
            merged = _merge_single_all_of(entry=strict_entry, parent=json_schema)
            json_schema.clear()
            json_schema.update(merged)
            return _ensure_strict_json_schema(
                json_schema,
                path=path,
                root=root,
                budget=budget,
                depth=next_depth,
                inside_nested_resource=inside_nested_resource,
            )
        else:
            json_schema["allOf"] = [
                _ensure_strict_json_schema(
                    entry,
                    path=(*path, "allOf", str(i)),
                    root=root,
                    budget=budget,
                    depth=next_depth,
                    inside_nested_resource=inside_nested_resource,
                )
                for i, entry in enumerate(all_of)
            ]

    # strip `None` defaults as there's no meaningful distinction here
    # the schema will still be `nullable` and the model will default
    # to using `None` anyway
    if json_schema.get("default", NOT_GIVEN) is None:
        json_schema.pop("default")

    # we can't use `$ref`s if there are also other properties defined, e.g.
    # `{"$ref": "...", "description": "my description"}`
    #
    # so we unravel the ref
    # `{"type": "string", "description": "my description"}`
    ref = json_schema.get("$ref")
    if ref and has_more_than_n_keys(json_schema, 1):
        assert isinstance(ref, str), f"Received non-string $ref - {ref}"

        resolved = resolve_ref(root=root, ref=ref)
        if not is_dict(resolved):
            raise ValueError(
                f"Expected `$ref: {ref}` to resolve to a dictionary but got {resolved}"
            )

        # Pop the current `$ref` first so that if the resolved schema is itself a `$ref`
        # (chained refs), we preserve it for the recursive expansion below instead of
        # silently dropping it.
        json_schema.pop("$ref")
        # properties from the json schema take priority over the ones on the `$ref`
        json_schema.update({**resolved, **json_schema})
        # Since the schema expanded from `$ref` might not have `additionalProperties: false` applied
        # we call `_ensure_strict_json_schema` again to fix the inlined schema and ensure it's valid
        return _ensure_strict_json_schema(
            json_schema,
            path=path,
            root=root,
            budget=budget,
            depth=next_depth,
            inside_nested_resource=inside_nested_resource,
        )

    if budget.reject_open_objects and "$ref" in json_schema:
        raise UserError(_UNVALIDATED_REF_ERROR)

    return json_schema


def resolve_ref(*, root: dict[str, object], ref: str) -> object:
    if not ref.startswith("#/"):
        raise ValueError(f"Unexpected $ref format {ref!r}; Does not start with #/")

    path = ref[2:].split("/")
    resolved = root
    for raw_key in path:
        key = raw_key.replace("~1", "/").replace("~0", "~")
        value = resolved[key]
        assert is_dict(value), (
            f"encountered non-dictionary entry while resolving {ref} - {resolved}"
        )
        resolved = value
        if _declares_schema_resource(resolved):
            raise UserError(_NESTED_RESOURCE_REF_ERROR)

    return resolved


def _declares_schema_resource(schema: dict[str, object]) -> bool:
    return isinstance(schema.get("$id"), str)


def _resolve_non_constraining_ref_chain(
    *,
    root: dict[str, object],
    schema: dict[str, Any],
    budget: _NodeBudget,
) -> dict[str, Any]:
    resolved = schema
    seen_refs: set[str] = set()
    carried_siblings: dict[str, Any] = {}
    while True:
        if "$ref" not in resolved:
            break
        if set(resolved) - {"$ref"} - _REF_NON_CONSTRAINING_SIBLINGS:
            break

        ref = resolved["$ref"]
        assert isinstance(ref, str), f"Received non-string $ref - {ref}"
        if ref in seen_refs:
            raise UserError("JSON schema contains a circular `$ref` chain.")
        seen_refs.add(ref)

        carried_siblings = {
            **{key: value for key, value in resolved.items() if key != "$ref"},
            **carried_siblings,
        }
        budget.spend()
        target = resolve_ref(root=root, ref=ref)
        if not is_dict(target):
            raise ValueError(f"Expected `$ref: {ref}` to resolve to a dictionary but got {target}")
        resolved = target
    return {**resolved, **carried_siblings}


def _merge_single_all_of(
    *,
    entry: dict[str, Any],
    parent: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(entry)
    incompatible_overlaps: list[str] = []
    for key, parent_value in parent.items():
        if key not in merged:
            merged[key] = parent_value
        elif key in _REF_NON_CONSTRAINING_SIBLINGS:
            merged[key] = parent_value
        elif _json_values_equal(parent_value, merged[key]):
            continue
        elif (key == "properties" and parent_value == {}) or (
            key == "required" and parent_value == []
        ):
            continue
        else:
            incompatible_overlaps.append(key)

    if incompatible_overlaps:
        raise UserError(
            "JSON schema contains a singleton `allOf` entry with incompatible parent "
            f"keyword(s) ({', '.join(sorted(incompatible_overlaps))}) that cannot be merged "
            "without changing its accepted values."
        )
    return merged


def _json_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=False)
        )
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if type(left) is not type(right):
        return False
    return cast(bool, left == right)


def is_dict(obj: object) -> TypeGuard[dict[str, object]]:
    # just pretend that we know there are only `str` keys
    # as that check is not worth the performance cost
    return isinstance(obj, dict)


def is_list(obj: object) -> TypeGuard[list[object]]:
    return isinstance(obj, list)


def has_more_than_n_keys(obj: dict[str, object], n: int) -> bool:
    i = 0
    for _ in obj.keys():
        i += 1
        if i > n:
            return True
    return False
