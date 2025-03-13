from __future__ import annotations

from typing import Any, Dict, List, Union

from openai import NOT_GIVEN
from typing_extensions import TypeGuard

from .exceptions import UserError

_EMPTY_SCHEMA: Dict[str, Any] = {
    "additionalProperties": False,
    "type": "object",
    "properties": {},
    "required": [],
}


def ensure_strict_json_schema(
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    """Mutates the given JSON schema to ensure it conforms to the `strict` standard
    that the OpenAI API expects.
    """
    if schema == {}:
        return _EMPTY_SCHEMA
    return _ensure_strict_json_schema(schema, path=(), root=schema)


def _ensure_strict_json_schema(
    json_schema: Dict[str, Any],
    *,
    path: tuple[str, ...],
    root: Dict[str, Any],
) -> Dict[str, Any]:
    """Ensures that the given JSON schema conforms to the `strict` standard.

    Args:
        json_schema: The JSON schema to ensure.
        path: The path to the current schema.
        root: The root schema.

    Returns:
        The ensured JSON schema.

    Raises:
        TypeError: If the given JSON schema is not a dictionary.
        UserError: If additionalProperties is set to True for object types.
    """
    if not is_dict(json_schema):
        raise TypeError(f"Expected {json_schema} to be a dictionary; path={path}")

    defs = json_schema.get("$defs")
    if is_dict(defs):
        for def_name, def_schema in defs.items():
            _ensure_strict_json_schema(def_schema, path=(*path, "$defs", def_name), root=root)

    definitions = json_schema.get("definitions")
    if is_dict(definitions):
        for definition_name, definition_schema in definitions.items():
            _ensure_strict_json_schema(
                definition_schema, path=(*path, "definitions", definition_name), root=root
            )

    typ = json_schema.get("type")
    if typ == "object" and "additionalProperties" not in json_schema:
        json_schema["additionalProperties"] = False
    elif (
        typ == "object"
        and "additionalProperties" in json_schema
        and json_schema["additionalProperties"] is True
    ):
        raise UserError(
            "additionalProperties should not be set for object types. This could be because "
            "you're using an older version of Pydantic, or because you configured additional "
            "properties to be allowed. If you really need this, update the function or output tool "
            "to not use a strict schema."
        )

    properties = json_schema.get("properties")
    if is_dict(properties):
        json_schema["required"] = list(properties.keys())
        json_schema["properties"] = {
            key: _ensure_strict_json_schema(prop_schema, path=(*path, "properties", key), root=root)
            for key, prop_schema in properties.items()
        }

    items = json_schema.get("items")
    if is_dict(items):
        json_schema["items"] = _ensure_strict_json_schema(items, path=(*path, "items"), root=root)

    any_of = json_schema.get("anyOf")
    if is_list(any_of):
        json_schema["anyOf"] = [
            _ensure_strict_json_schema(variant, path=(*path, "anyOf", str(i)), root=root)
            for i, variant in enumerate(any_of)
        ]

    all_of = json_schema.get("allOf")
    if is_list(all_of):
        if len(all_of) == 1:
            json_schema.update(
                _ensure_strict_json_schema(all_of[0], path=(*path, "allOf", "0"), root=root)
            )
            json_schema.pop("allOf")
        else:
            json_schema["allOf"] = [
                _ensure_strict_json_schema(entry, path=(*path, "allOf", str(i)), root=root)
                for i, entry in enumerate(all_of)
            ]

    if json_schema.get("default", NOT_GIVEN) is None:
        json_schema.pop("default")

    ref = json_schema.get("$ref")
    if ref and has_more_than_n_keys(json_schema, 1):
        assert isinstance(ref, str), f"Received non-string $ref - {ref}"

        resolved = resolve_ref(root=root, ref=ref)
        if not is_dict(resolved):
            raise ValueError(
                f"Expected `$ref: {ref}` to resolved to a dictionary but got {resolved}"
            )

        json_schema.update({**resolved, **json_schema})
        json_schema.pop("$ref")
        return _ensure_strict_json_schema(json_schema, path=path, root=root)

    return json_schema


def resolve_ref(*, root: Dict[str, Any], ref: str) -> Any:
    """Resolves a JSON schema reference.

    Args:
        root: The root schema.
        ref: The reference to resolve.

    Returns:
        The resolved reference.

    Raises:
        ValueError: If the reference format is unexpected.
    """
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


def is_dict(obj: Any) -> TypeGuard[Dict[str, Any]]:
    """Checks if the given object is a dictionary.

    Args:
        obj: The object to check.

    Returns:
        True if the object is a dictionary, False otherwise.
    """
    return isinstance(obj, dict)


def is_list(obj: Any) -> TypeGuard[List[Any]]:
    """Checks if the given object is a list.

    Args:
        obj: The object to check.

    Returns:
        True if the object is a list, False otherwise.
    """
    return isinstance(obj, list)


def has_more_than_n_keys(obj: Dict[str, Any], n: int) -> bool:
    """Checks if the given dictionary has more than n keys.

    Args:
        obj: The dictionary to check.
        n: The number of keys to compare against.

    Returns:
        True if the dictionary has more than n keys, False otherwise.
    """
    i = 0
    for _ in obj.keys():
        i += 1
        if i > n:
            return True
    return False
