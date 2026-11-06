"""Retain import and pickle identities while implementations live privately."""

import sys
from collections.abc import Callable
from types import FunctionType
from typing import Any, get_type_hints

from typing_extensions import TypeAliasType, is_typeddict


def _new_type_alias(name: str, value: Any, constructor: Callable[[str, Any], Any]) -> Any:
    return constructor(name, value)


def type_alias_constructor(public_module: str) -> Callable[[str, Any], Any]:
    """Create immutable aliases with their public owner from the start."""
    # TypeAliasType reads its caller's module. Keep the runtime globals intact.
    return FunctionType(
        _new_type_alias.__code__,
        {"__name__": public_module},
        argdefs=(TypeAliasType,),
    )


def preserve_module_identity(namespace: dict[str, Any], public_module: str) -> None:
    """Keep existing owners for definitions and their method descriptors."""
    implementation_module = namespace["__name__"]
    # Resolve every schema before changing any base class's deferred annotations.
    schemas = [
        (value, get_type_hints(value, globalns=namespace, localns=namespace, include_extras=True))
        for value in namespace.values()
        if is_typeddict(value) and value.__module__ == implementation_module
    ]
    for value, annotations in schemas:
        value.__annotations__ = annotations

    def restore(value: Any) -> None:
        if isinstance(value, (staticmethod, classmethod)):
            restore(value.__func__)
        elif isinstance(value, property):
            for accessor in (value.fget, value.fset, value.fdel):
                restore(accessor)
        elif getattr(value, "__module__", None) == implementation_module:
            if sys.version_info >= (3, 14) and is_typeddict(value):
                # Resolve deferred fields before schema readers look in the public facade,
                # which may not have loaded while private modules import each other.
                value.__annotations__.update(get_type_hints(value, include_extras=True))
            value.__module__ = public_module
            if isinstance(value, type):
                for member in vars(value).values():
                    restore(member)
            elif wrapped := getattr(value, "__wrapped__", None):
                restore(wrapped)

    for value in namespace.values():
        restore(value)
