from __future__ import annotations

import copy
from typing import Any, TypeGuard

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

# Keywords that may legally sit alongside a `$ref` without changing the accepted values:
# JSON Schema annotation keywords plus definition maps (which are inert metadata). Any
# other sibling of a `$ref` is a *constraining* keyword (e.g. `properties`, `required`,
# `type`, `enum`), which JSON Schema 2020-12 combines with the referent as an
# intersection rather than as an override.
_REF_ANNOTATION_SIBLINGS = frozenset(
    {
        "$comment",
        "$defs",
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

# Keywords OpenAI's strict mode cannot represent. Mirrors the unsupported-keyword list in
# the vendor reference implementation (openai-node `toStrictJsonSchema`); a schema that
# still contains any of these after conversion is not convertible and must be rejected so
# callers (e.g. the MCP `convert_schemas_to_strict` path) fall back to the original
# non-strict schema instead of shipping one the API will reject at request time.
_STRICT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "$anchor",
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveAnchor",
        "$recursiveRef",
        "allOf",
        "contains",
        "contentEncoding",
        "contentMediaType",
        "contentSchema",
        "dependentRequired",
        "dependentSchemas",
        "dependencies",
        "else",
        "if",
        "maxContains",
        "maxProperties",
        "minContains",
        "minProperties",
        "not",
        "patternProperties",
        "prefixItems",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
    }
)


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
    if schema == {}:
        return copy.deepcopy(_EMPTY_SCHEMA)
    budget = _NodeBudget(_MAX_SCHEMA_NODES, reject_open_objects=_reject_open_objects)
    result = _ensure_strict_root(
        _ensure_strict_json_schema(
            schema,
            path=(),
            root=schema,
            budget=budget,
        )
    )
    _raise_if_strict_unsupported(result)
    return result


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


def _raise_if_strict_unsupported(schema: dict[str, Any], *, path: tuple[str, ...] = ()) -> None:
    """Raises ``UserError`` if any keyword OpenAI's strict mode cannot represent survived
    conversion, so callers fall back to the original non-strict schema instead of shipping
    one the API will reject at request time.
    """
    location = "/" + "/".join(path) if path else "<root>"
    for keyword in _STRICT_UNSUPPORTED_KEYWORDS:
        if keyword in schema:
            raise UserError(
                f"JSON schema at {location} contains keyword `{keyword}` which cannot be "
                "represented in a strict schema."
            )

    # Tuple-form `items` (an array of schemas) has no strict-mode equivalent.
    items = schema.get("items")
    if is_list(items):
        raise UserError(
            f"JSON schema at {location} uses tuple-form `items`, which cannot be represented "
            "in a strict schema."
        )

    # Draft 7 `additionalItems` only exists to extend a tuple; the API accepts one schema
    # per array item and cannot represent it.
    if "additionalItems" in schema:
        raise UserError(
            f"JSON schema at {location} uses keyword `additionalItems`, which cannot be "
            "represented in a strict schema."
        )

    # An array with no `items` accepts arbitrary elements and is not strict.
    typ = schema.get("type")
    if (typ == "array" or (is_list(typ) and "array" in typ)) and "items" not in schema:
        raise UserError(
            f"JSON schema at {location} is an array without `items`, which cannot be "
            "represented in a strict schema."
        )

    # A nested `$id` re-identifies a subschema and is not supported outside the root.
    if path and "$id" in schema:
        raise UserError(
            f"JSON schema at {location} contains a nested `$id`, which cannot be represented "
            "in a strict schema."
        )

    # Recurse into every schema-bearing container.
    for container in ("$defs", "definitions", "properties"):
        subschemas = schema.get(container)
        if is_dict(subschemas):
            for name, child in subschemas.items():
                _raise_if_strict_unsupported(child, path=(*path, container, name))
    for container in ("items", "additionalProperties", "not", "if", "then", "else"):
        child = schema.get(container)
        if is_dict(child):
            _raise_if_strict_unsupported(child, path=(*path, container))
    for container in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(container)
        if is_list(variants):
            for i, variant in enumerate(variants):
                _raise_if_strict_unsupported(variant, path=(*path, container, str(i)))


# Adapted from https://github.com/openai/openai-python/blob/main/src/openai/lib/_pydantic.py
def _ensure_strict_json_schema(
    json_schema: object,
    *,
    path: tuple[str, ...],
    root: dict[str, object],
    budget: _NodeBudget | None = None,
) -> dict[str, Any]:
    if not is_dict(json_schema):
        raise TypeError(f"Expected {json_schema} to be a dictionary; path={path}")

    # Bound the total number of nodes we expand so a malicious `$ref` fan-out cannot expand
    # exponentially and exhaust CPU and memory.
    if budget is None:
        budget = _NodeBudget(_MAX_SCHEMA_NODES)
    budget.spend()

    # A `$ref` with a *constraining* sibling is an intersection in JSON Schema 2020-12:
    # a value must satisfy both the referent and the sibling. Inlining it parent-wins
    # (as the code below does for annotation-only siblings) would silently delete the
    # referent's own constraints and invert the accepted set, so reject it loudly.
    if "$ref" in json_schema:
        constraining_siblings = set(json_schema.keys()) - {"$ref"} - _REF_ANNOTATION_SIBLINGS
        if constraining_siblings:
            raise UserError(
                "JSON schema contains a `$ref` with constraining sibling keyword(s) "
                f"({', '.join(sorted(constraining_siblings))}) that cannot be represented "
                "in a strict schema without changing its accepted values."
            )

    defs = json_schema.get("$defs")
    if is_dict(defs):
        for def_name, def_schema in defs.items():
            _ensure_strict_json_schema(
                def_schema, path=(*path, "$defs", def_name), root=root, budget=budget
            )

    definitions = json_schema.get("definitions")
    if is_dict(definitions):
        for definition_name, definition_schema in definitions.items():
            _ensure_strict_json_schema(
                definition_schema,
                path=(*path, "definitions", definition_name),
                root=root,
                budget=budget,
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
                prop_schema, path=(*path, "properties", key), root=root, budget=budget
            )
            for key, prop_schema in properties.items()
        }

    # arrays
    # { 'type': 'array', 'items': {...} }
    items = json_schema.get("items")
    if is_dict(items):
        json_schema["items"] = _ensure_strict_json_schema(
            items, path=(*path, "items"), root=root, budget=budget
        )

    # unions
    any_of = json_schema.get("anyOf")
    if is_list(any_of):
        json_schema["anyOf"] = [
            _ensure_strict_json_schema(
                variant, path=(*path, "anyOf", str(i)), root=root, budget=budget
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
                variant, path=(*path, "oneOf", str(i)), root=root, budget=budget
            )
            for i, variant in enumerate(one_of)
        ]
        json_schema.pop("oneOf")

    # intersections
    all_of = json_schema.get("allOf")
    if is_list(all_of):
        if len(all_of) == 1:
            json_schema.update(
                _ensure_strict_json_schema(
                    all_of[0], path=(*path, "allOf", "0"), root=root, budget=budget
                )
            )
            json_schema.pop("allOf")
            return _ensure_strict_json_schema(json_schema, path=path, root=root, budget=budget)
        else:
            json_schema["allOf"] = [
                _ensure_strict_json_schema(
                    entry, path=(*path, "allOf", str(i)), root=root, budget=budget
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
                f"Expected `$ref: {ref}` to resolved to a dictionary but got {resolved}"
            )

        # Pop the current `$ref` first so that if the resolved schema is itself a `$ref`
        # (chained refs), we preserve it for the recursive expansion below instead of
        # silently dropping it.
        json_schema.pop("$ref")
        # properties from the json schema take priority over the ones on the `$ref`
        json_schema.update({**resolved, **json_schema})
        # Since the schema expanded from `$ref` might not have `additionalProperties: false` applied
        # we call `_ensure_strict_json_schema` again to fix the inlined schema and ensure it's valid
        return _ensure_strict_json_schema(json_schema, path=path, root=root, budget=budget)

    if budget.reject_open_objects and "$ref" in json_schema:
        raise UserError(_UNVALIDATED_REF_ERROR)

    return json_schema


def resolve_ref(*, root: dict[str, object], ref: str) -> object:
    if not ref.startswith("#/"):
        raise ValueError(f"Unexpected $ref format {ref!r}; Does not start with #/")

    path = ref[2:].split("/")
    resolved = root
    for key in path:
        value = resolved[key]
        assert is_dict(value), (
            f"encountered non-dictionary entry while resolving {ref} - {resolved}"
        )
        resolved = value

    return resolved


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
