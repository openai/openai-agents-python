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
        f"{export_name} requires '{dependency_name}'. "
        "From the matching source checkout, run "
        f"`python -m pip install '.[classic-{extra_name}]'`. "
        "See CLASSIC.md#install-from-the-matching-source."
    )
    if cause is None:
        raise error
    raise error from cause
