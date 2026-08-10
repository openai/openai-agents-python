from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from urllib.parse import parse_qsl, unquote, urlsplit

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _url_contains_inline_authority(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    if "@" in value:
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None or bool(parsed.query)


def _inline_url_authority_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not _url_contains_inline_authority(value):
        return ()

    values = [value]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return tuple(values)

    for authority_value in (parsed.username, parsed.password, parsed.query):
        if authority_value:
            values.extend((authority_value, unquote(authority_value)))
    for _key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        if query_value:
            values.extend((query_value, unquote(query_value)))
    return tuple(dict.fromkeys(values))


def _redact_mount_lifecycle_error(
    function: Callable[_P, Coroutine[Any, Any, _T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Load the mount error boundary lazily to avoid an entries/security import cycle."""

    protected: Callable[_P, Coroutine[Any, Any, _T]] | None = None

    @wraps(function)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        nonlocal protected
        if protected is None:
            from ..._mount_security import redact_mount_error_data

            protected = redact_mount_error_data(function)
        try:
            return await protected(*args, **kwargs)
        except BaseException:
            del args, kwargs
            raise

    return wrapper
