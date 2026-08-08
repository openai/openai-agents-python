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

_FREE_FORM_OBJECT_ERROR = (
    "Strict JSON schemas cannot express this object. An object that declares no properties at "
    "its own level and no explicit additionalProperties: false accepts arbitrary keys, which "
    "strict mode does not support. Describe the expected properties, set additionalProperties "
    "to false if the empty object really is the only valid value, or update the function or "
    "output tool to not use a strict schema."
)


class _NodeBudget:
    """Carries per-conversion state: the node budget and the free-form definition registry.

    Riding on this object rather than a separate parameter guarantees every recursive call
    sees the same registry, since the budget must already reach every call for the DoS bound.
    """

    def __init__(self, limit: int) -> None:
        self.remaining = limit
        # Definitions that were free-form before the definition walk closed them, recorded by
        # object identity so nested `$defs` need no path bookkeeping. A `$ref` to one of these
        # is salvageable when its siblings supply the shape.
        self.free_form_definition_ids: set[int] = set()
        # Definitions whose interior holds a free-form node at a value position. No sibling
        # merge can reach inside the referenced subtree, so a `$ref` to one of these is never
        # salvageable.
        self.tainted_definition_ids: set[int] = set()
        # Identity of the definition currently being walked, innermost last.
        self.definition_stack: list[int] = []

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
) -> dict[str, Any]:
    """Mutates the given JSON schema to ensure it conforms to the `strict` standard
    that the OpenAI API expects.
    """
    if schema == {}:
        return copy.deepcopy(_EMPTY_SCHEMA)
    budget = _NodeBudget(_MAX_SCHEMA_NODES)
    _precompute_definition_registries(schema, budget)
    converted = _ensure_strict_json_schema(schema, path=(), root=schema, budget=budget)
    return _ensure_strict_root(converted)


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
# Keywords that describe an object's contents somewhere other than its own `properties` map.
# An empty `properties` map next to one of these is not a statement that the object is empty.
_CONTENT_SHAPING_KEYWORDS = frozenset(
    {
        "allOf",
        "anyOf",
        "const",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "enum",
        "if",
        "not",
        "oneOf",
        "maxProperties",
        "minProperties",
        "patternProperties",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    }
)


def _is_unclosable_object(json_schema: dict[str, object]) -> bool:
    """Whether closing this object with ``additionalProperties: false`` would change meaning."""
    typ = json_schema.get("type")
    declared_properties = json_schema.get("properties")
    # A typeless schema with a properties map is normalized to an object by the conversion
    # walk, so the precomputed registries must see it the same way.
    is_object = (
        typ == "object"
        or (is_list(typ) and "object" in typ)
        or (typ is None and is_dict(declared_properties))
    )
    if not is_object or "additionalProperties" in json_schema:
        return False
    if _allows_only_the_empty_object(json_schema):
        return False
    declared = json_schema.get("properties")
    if not is_dict(declared):
        return True
    return not declared and _shapes_contents_without_properties(json_schema)


def _shapes_contents_without_properties(json_schema: dict[str, object]) -> bool:
    return any(keyword in json_schema for keyword in _CONTENT_SHAPING_KEYWORDS)


def _allows_only_the_empty_object(json_schema: dict[str, object]) -> bool:
    """Whether the schema already forbids every key, so closing it changes nothing."""
    if json_schema.get("maxProperties") == 0:
        return True
    if json_schema.get("const") == {}:
        return True
    enum = json_schema.get("enum")
    return is_list(enum) and bool(enum) and all(entry == {} for entry in enum)


def _siblings_supply_the_shape(siblings: dict[str, object]) -> bool:
    """Whether the keys alongside a `$ref` to a free-form definition close or shape it."""
    declared = siblings.get("properties")
    if is_dict(declared) and declared:
        return True
    if "additionalProperties" in siblings:
        return True
    return _allows_only_the_empty_object(siblings)


def _resolve_ref_chain(ref: str, *, root: dict[str, object]) -> object | None:
    """Resolve a ref, following alias definitions that are themselves a lone `$ref`."""
    seen: set[str] = set()
    current = ref
    while current not in seen:
        seen.add(current)
        try:
            resolved = resolve_ref(root=root, ref=current)
        except Exception:
            # Unresolvable refs keep their historical handling; they are not this check's concern.
            return None
        if not is_dict(resolved):
            return None
        next_ref = resolved.get("$ref")
        if isinstance(next_ref, str) and not has_more_than_n_keys(resolved, 1):
            current = next_ref
            continue
        return resolved
    return None


def _resolves_to_free_form_definition(
    ref: str, *, root: dict[str, object], budget: _NodeBudget
) -> bool:
    resolved = _resolve_ref_chain(ref, root=root)
    if resolved is None:
        return False
    return (
        id(resolved) in budget.free_form_definition_ids
        or id(resolved) in budget.tainted_definition_ids
    )


_DEFINITION_CONTAINER_KEYS = ("$defs", "definitions")

# Keywords whose values are data literals, not schemas. A literal such as
# `{"const": {"type": "object"}}` must never have its value mistaken for a schema node.
_LITERAL_VALUE_KEYWORDS = frozenset({"const", "default", "enum", "examples"})


def _iter_schema_nodes(root: object, *, limit: int) -> list[dict[str, object]]:
    """Every dict node in the tree, cycle-safe and bounded like the conversion itself."""
    nodes: list[dict[str, object]] = []
    stack: list[object] = [root]
    seen: set[int] = set()
    count = 0
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        count += 1
        if count > limit:
            raise UserError(
                "JSON schema is too large to convert to a strict schema. This can happen when a "
                "schema expands `$ref`s exponentially, which may indicate a malformed or "
                "malicious schema."
            )
        if is_dict(node):
            nodes.append(node)
            stack.extend(value for key, value in node.items() if key not in _LITERAL_VALUE_KEYWORDS)
        elif is_list(node):
            stack.extend(node)
    return nodes


def _node_is_unshaped_ref(node: dict[str, object]) -> bool:
    """A `$ref` node whose sibling keys do not shape or close the referenced object."""
    return isinstance(node.get("$ref"), str) and not _siblings_supply_the_shape(
        {key: value for key, value in node.items() if key != "$ref"}
    )


def _precompute_definition_registries(root: dict[str, object], budget: _NodeBudget) -> None:
    """Populate the free-form and tainted registries before anything is walked or mutated.

    Populating during the definition walk is order-dependent: an alias that appears before
    its target would be inlined and closed while the target is still unrecorded. This pass
    reads the untouched tree, seeds both registries from what each definition says on its
    own, then iterates alias and interior references to a fixpoint, so declaration order
    cannot matter.
    """
    definition_entries: list[dict[str, object]] = []
    for node in _iter_schema_nodes(root, limit=_MAX_SCHEMA_NODES):
        for container_key in _DEFINITION_CONTAINER_KEYS:
            container = node.get(container_key)
            if is_dict(container):
                definition_entries.extend(entry for entry in container.values() if is_dict(entry))

    interior_nodes: dict[int, list[dict[str, object]]] = {}
    for entry in definition_entries:
        # A definition's own nested definition containers are separate templates and do not
        # shape this definition's value, so they are excluded from its interior.
        interior: list[dict[str, object]] = []
        stack: list[object] = [
            value
            for key, value in entry.items()
            if key not in _DEFINITION_CONTAINER_KEYS and key not in _LITERAL_VALUE_KEYWORDS
        ]
        seen: set[int] = set()
        while stack:
            interior_node = stack.pop()
            if id(interior_node) in seen:
                continue
            seen.add(id(interior_node))
            if is_dict(interior_node):
                interior.append(interior_node)
                stack.extend(
                    value
                    for key, value in interior_node.items()
                    if key not in _DEFINITION_CONTAINER_KEYS and key not in _LITERAL_VALUE_KEYWORDS
                )
            elif is_list(interior_node):
                stack.extend(interior_node)
        interior_nodes[id(entry)] = interior

        if _is_unclosable_object(entry):
            budget.free_form_definition_ids.add(id(entry))
        for node in interior:
            if _is_unclosable_object(node):
                budget.tainted_definition_ids.add(id(entry))
                break
            required = node.get("required")
            properties = node.get("properties")
            if (
                "$ref" not in node
                and is_dict(properties)
                and is_list(required)
                and any(name not in properties for name in required)
            ):
                budget.tainted_definition_ids.add(id(entry))
                break

    changed = True
    while changed:
        changed = False
        for entry in definition_entries:
            entry_id = id(entry)
            if entry_id in budget.tainted_definition_ids:
                continue
            root_ref = entry.get("$ref")
            if isinstance(root_ref, str) and entry_id not in budget.free_form_definition_ids:
                target = _resolve_ref_chain(root_ref, root=root)
                if target is not None:
                    if id(target) in budget.tainted_definition_ids:
                        # No sibling merge reaches inside the referenced subtree.
                        budget.tainted_definition_ids.add(entry_id)
                        changed = True
                        continue
                    if id(target) in budget.free_form_definition_ids and _node_is_unshaped_ref(
                        entry
                    ):
                        budget.free_form_definition_ids.add(entry_id)
                        changed = True
            for node in interior_nodes[entry_id]:
                if node is entry:
                    continue
                node_ref = node.get("$ref")
                if not isinstance(node_ref, str):
                    continue
                target = _resolve_ref_chain(node_ref, root=root)
                if target is None:
                    continue
                if id(target) in budget.tainted_definition_ids or (
                    id(target) in budget.free_form_definition_ids and _node_is_unshaped_ref(node)
                ):
                    budget.tainted_definition_ids.add(entry_id)
                    changed = True
                    break


def _ensure_strict_json_schema(
    json_schema: object,
    *,
    path: tuple[str, ...],
    root: dict[str, object],
    budget: _NodeBudget | None = None,
    in_definitions: bool = False,
) -> dict[str, Any]:
    if not is_dict(json_schema):
        raise TypeError(f"Expected {json_schema} to be a dictionary; path={path}")

    # Bound the total number of nodes we expand so a malicious `$ref` fan-out cannot expand
    # exponentially and exhaust CPU and memory.
    if budget is None:
        budget = _NodeBudget(_MAX_SCHEMA_NODES)
        _precompute_definition_registries(root, budget)
    budget.spend()

    defs = json_schema.get("$defs")
    if is_dict(defs):
        for def_name, def_schema in defs.items():
            if is_dict(def_schema) and _is_unclosable_object(def_schema):
                # Remember it before the walk below closes it, so a `$ref` that points here
                # can be handled rather than silently narrowed to the empty object.
                budget.free_form_definition_ids.add(id(def_schema))
            budget.definition_stack.append(id(def_schema))
            try:
                _ensure_strict_json_schema(
                    def_schema,
                    path=(*path, "$defs", def_name),
                    root=root,
                    budget=budget,
                    in_definitions=True,
                )
            finally:
                budget.definition_stack.pop()

    definitions = json_schema.get("definitions")
    if is_dict(definitions):
        for definition_name, definition_schema in definitions.items():
            if is_dict(definition_schema) and _is_unclosable_object(definition_schema):
                # Remember it before the walk below closes it, so a `$ref` that points here
                # can be handled rather than silently narrowed to the empty object.
                budget.free_form_definition_ids.add(id(definition_schema))
            budget.definition_stack.append(id(definition_schema))
            try:
                _ensure_strict_json_schema(
                    definition_schema,
                    path=(*path, "definitions", definition_name),
                    root=root,
                    budget=budget,
                    in_definitions=True,
                )
            finally:
                budget.definition_stack.pop()

    typ = json_schema.get("type")
    properties = json_schema.get("properties")
    if typ is None and is_dict(properties):
        typ = json_schema["type"] = "object"
    elif typ is None and json_schema.get("additionalProperties", False) is not False:
        raise UserError(_ADDITIONAL_PROPERTIES_ERROR)
    is_object = typ == "object" or (is_list(typ) and "object" in typ)
    if (
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
        all_of_value = json_schema.get("allOf")
        merge_is_pending = "$ref" in json_schema or (
            is_list(all_of_value) and len(all_of_value) == 1
        )
        if merge_is_pending:
            # A `$ref` or single-entry `allOf` merge will land more properties on this node
            # and re-enter, so required handling must wait for the merged shape. Overwriting
            # now would also destroy the original `required` before it can be judged.
            pass
        else:
            declared_required = json_schema.get("required")
            if is_list(declared_required) and any(
                name not in properties for name in declared_required
            ):
                # The object requires keys it never declares, so their values are
                # unconstrained. Strict mode needs required to be a subset of properties with
                # everything else forbidden, so conversion would silently drop the requirement
                # and forbid the key.
                if in_definitions:
                    if budget.definition_stack:
                        budget.tainted_definition_ids.add(budget.definition_stack[-1])
                else:
                    raise UserError(_FREE_FORM_OBJECT_ERROR)
        if not merge_is_pending:
            json_schema["required"] = list(properties.keys())
        json_schema["properties"] = {
            key: _ensure_strict_json_schema(
                prop_schema,
                path=(*path, "properties", key),
                root=root,
                budget=budget,
                in_definitions=in_definitions,
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
            in_definitions=in_definitions,
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
                in_definitions=in_definitions,
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
                in_definitions=in_definitions,
            )
            for i, variant in enumerate(one_of)
        ]
        json_schema.pop("oneOf")

    # intersections
    all_of = json_schema.get("allOf")
    if is_list(all_of):
        if len(all_of) == 1:
            entry = all_of[0]
            if not is_dict(entry):
                raise TypeError(
                    f"Expected {entry} to be a dictionary; path={(*path, 'allOf', '0')}"
                )
            # Merge the single branch before converting it, then re-enter. Converting the branch
            # on its own first would judge it in isolation, so a branch that constrains nothing
            # by itself, such as a redundant `{"type": "object"}` next to a parent that already
            # declares `properties`, would look like a free-form object and be rejected.
            json_schema.pop("allOf")
            json_schema.update(entry)
            return _ensure_strict_json_schema(
                json_schema,
                path=path,
                root=root,
                budget=budget,
                in_definitions=in_definitions,
            )
        else:
            json_schema["allOf"] = [
                _ensure_strict_json_schema(
                    entry,
                    path=(*path, "allOf", str(i)),
                    root=root,
                    budget=budget,
                    in_definitions=in_definitions,
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
    if isinstance(ref, str) and not has_more_than_n_keys(json_schema, 1):
        if _resolves_to_free_form_definition(ref, root=root, budget=budget):
            if in_definitions:
                # Inside a template this only matters if something references the template,
                # so record it rather than failing a possibly unreferenced definition.
                if budget.definition_stack:
                    budget.tainted_definition_ids.add(budget.definition_stack[-1])
            else:
                # A bare `$ref` is never inlined, so the strict schema would keep pointing at
                # a definition that the walk above has since closed into the empty object.
                raise UserError(_FREE_FORM_OBJECT_ERROR)
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
        reference_cannot_be_strict = id(resolved) in budget.tainted_definition_ids or (
            id(resolved) in budget.free_form_definition_ids
            and not _siblings_supply_the_shape(json_schema)
        )
        if reference_cannot_be_strict and in_definitions:
            # Same as the bare-ref case: a template is only a problem once referenced.
            if budget.definition_stack:
                budget.tainted_definition_ids.add(budget.definition_stack[-1])
        elif id(resolved) in budget.tainted_definition_ids:
            # The free-form node is inside the referenced subtree, out of reach of any
            # sibling merge, so this reference can never be made strict.
            raise UserError(_FREE_FORM_OBJECT_ERROR)
        elif id(resolved) in budget.free_form_definition_ids and not _siblings_supply_the_shape(
            json_schema
        ):
            # The definition was free-form, so the keys alongside the `$ref` must supply the
            # shape. An empty `properties` map or a bare annotation does not, and the merged
            # result would inherit the `additionalProperties: false` the walk added and be
            # silently narrowed to the empty object.
            raise UserError(_FREE_FORM_OBJECT_ERROR)
        # properties from the json schema take priority over the ones on the `$ref`
        json_schema.update({**resolved, **json_schema})
        # Since the schema expanded from `$ref` might not have `additionalProperties: false` applied
        # we call `_ensure_strict_json_schema` again to fix the inlined schema and ensure it's valid
        return _ensure_strict_json_schema(
            json_schema,
            path=path,
            root=root,
            budget=budget,
            in_definitions=in_definitions,
        )

    # Decide whether this object can be closed only once the normalizations above have run, so
    # that a `$ref` or a single-entry `allOf` has already had the chance to lift `properties` up
    # to this level. Both of those paths re-enter this function and are handled by the return
    # statements above rather than reaching here.
    if is_object and "additionalProperties" not in json_schema and in_definitions:
        is_definition_root = bool(
            budget.definition_stack and budget.definition_stack[-1] == id(json_schema)
        )
        if (
            not is_definition_root
            and _is_unclosable_object(json_schema)
            and budget.definition_stack
        ):
            # A free-form node at a value position inside a definition can never be salvaged:
            # a sibling merge at the `$ref` site only reshapes the top level, not the interior.
            # Record the enclosing definition so any reference to it falls back.
            budget.tainted_definition_ids.add(budget.definition_stack[-1])

    if is_object and "additionalProperties" not in json_schema and not in_definitions:
        # A definition is a template rather than a value position: a broad base is routinely
        # narrowed by the keys a `$ref` site supplies, and an unreferenced one has no effect at
        # all. Judging one on its own would reject schemas that are perfectly strictable once
        # inlined, so definitions keep the historical behaviour and are closed below.
        declared = json_schema.get("properties")
        if _allows_only_the_empty_object(json_schema):
            # Already constrained to the empty object, so closing it changes nothing.
            pass
        elif not is_dict(declared):
            # Nothing at this level says which keys are allowed, so the object accepts any of
            # them. Closing it with `additionalProperties: false` would silently narrow it to
            # "the empty object is the only valid value", or, for a composed wrapper such as
            # `anyOf`/multi-branch `allOf`/`enum`/`const`, would reject the very values the
            # branches describe. Fail the same way an explicit `additionalProperties: true`
            # does, so callers that can degrade (such as MCP tool conversion) fall back to
            # serving the schema as non-strict.
            raise UserError(_FREE_FORM_OBJECT_ERROR)
        elif not declared and _shapes_contents_without_properties(json_schema):
            # An empty `properties` map next to a keyword that describes the contents some
            # other way is not a declaration that the object is empty.
            raise UserError(_FREE_FORM_OBJECT_ERROR)

    if is_object and "additionalProperties" not in json_schema:
        json_schema["additionalProperties"] = False

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
