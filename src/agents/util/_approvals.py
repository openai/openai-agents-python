from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any, NoReturn

from ..exceptions import UserError

# Keep this helper here so both run_internal and realtime can import it without
# creating cross-package dependencies.


def _reject_nonstandard_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


def parse_function_tool_arguments(arguments: str | None) -> dict[str, Any] | None:
    """Return parsed object arguments, or None when an approval policy cannot inspect them."""
    try:
        parsed = json.loads(
            arguments or "{}",
            parse_constant=_reject_nonstandard_json_constant,
        )
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def evaluate_needs_approval_setting(
    needs_approval_setting: bool | Callable[..., Any],
    *args: Any,
    default: bool = False,
    strict: bool = True,
) -> bool:
    """Return bool from a needs_approval setting that may be bool or callable/awaitable."""
    if isinstance(needs_approval_setting, bool):
        return needs_approval_setting
    if callable(needs_approval_setting):
        maybe_result = needs_approval_setting(*args)
        if inspect.isawaitable(maybe_result):
            maybe_result = await maybe_result
        return bool(maybe_result)
    if strict:
        raise UserError(
            f"Invalid needs_approval value: expected a bool or callable, "
            f"got {type(needs_approval_setting).__name__}."
        )
    return default
