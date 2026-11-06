"""Retain import and pickle identities while implementations live privately."""

from collections.abc import Callable
from types import FunctionType
from typing import Any

from typing_extensions import TypeAliasType


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

    def restore(value: Any) -> None:
        if isinstance(value, (staticmethod, classmethod)):
            restore(value.__func__)
        elif isinstance(value, property):
            for accessor in (value.fget, value.fset, value.fdel):
                restore(accessor)
        elif getattr(value, "__module__", None) == implementation_module:
            value.__module__ = public_module
            if isinstance(value, type):
                for member in vars(value).values():
                    restore(member)
            elif wrapped := getattr(value, "__wrapped__", None):
                restore(wrapped)

    for value in namespace.values():
        restore(value)
