"""Retain import and pickle identities while implementations live privately."""

from typing import Any


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
