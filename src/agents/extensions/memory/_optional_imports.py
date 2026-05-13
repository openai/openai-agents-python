from __future__ import annotations

from typing import NoReturn


def raise_optional_dependency_error(
    export_name: str,
    *,
    dependency_name: str,
    extra_name: str,
    cause: ImportError | None = None,
) -> NoReturn:
    error = ImportError(
        f"{export_name} requires the '{dependency_name}' extra. "
        f"Install it with: pip install openai-agents[{extra_name}]"
    )
    if cause is None:
        raise error
    raise error from cause
