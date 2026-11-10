"""Retain public module identities for shared definitions."""

import sys
from typing import Any

from typing_extensions import is_typeddict


def _resolve_typeddict_annotations(value: Any, namespace: dict[str, Any]) -> None:
    # Resolve known names before changing the module that owns the TypedDict.
    # FORWARDREF preserves valid references to names that are not yet defined.
    if sys.version_info >= (3, 14) and is_typeddict(value):
        from typing import evaluate_forward_ref

        from annotationlib import Format, ForwardRef, get_annotations

        value.__annotations__.update(
            {
                name: evaluate_forward_ref(annotation, globals=namespace, format=Format.FORWARDREF)
                if isinstance(annotation, ForwardRef)
                else annotation
                for name, annotation in get_annotations(value, format=Format.FORWARDREF).items()
            }
        )


def _preserve_legacy_module(namespace: dict[str, Any], legacy_module: str) -> None:
    """Keep existing type/function identities while both runtimes share one module."""
    for value in tuple(namespace.values()):
        if getattr(value, "__module__", None) == namespace["__name__"]:
            _resolve_typeddict_annotations(value, namespace)
            value.__module__ = legacy_module
