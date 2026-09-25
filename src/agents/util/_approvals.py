from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn

from .._function_tool_arguments import FunctionToolApproval
from ..exceptions import UserError

if TYPE_CHECKING:
    from ..run_context import RunContextWrapper
    from ..tool import FunctionTool

# Keep this helper here so both run_internal and realtime can import it without
# creating cross-package dependencies.


def _reject_nonstandard_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


def parse_function_tool_arguments(arguments: str | None) -> dict[str, Any] | None:
    """Return parsed object arguments, or None when an approval policy cannot inspect them."""
    if arguments is None or not arguments.strip():
        return None
    try:
        parsed = json.loads(
            arguments,
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


async def evaluate_function_tool_approval(
    function_tool: FunctionTool,
    context: RunContextWrapper[Any],
    arguments: str,
    call_id: str,
    *,
    strict: bool = True,
) -> FunctionToolApproval:
    """Evaluate a policy against unchanged input, retaining one prepared invocation."""
    from ..tool import _FailureHandlingFunctionToolInvoker

    invoker = function_tool.on_invoke_tool
    result = FunctionToolApproval("require_approval", function_tool, invoker, arguments)
    params: dict[str, Any] = {}
    if callable(function_tool.needs_approval):
        parsed = parse_function_tool_arguments(arguments)
        if parsed is None:
            return result
        params = parsed
        if isinstance(invoker, _FailureHandlingFunctionToolInvoker):
            result.prepared = invoker.prepare_arguments(arguments, function_tool.name)
            if result.prepared is not None:
                if not result.prepared.unchanged:
                    result.prepared = None
                    return result
    needs_approval = await evaluate_needs_approval_setting(
        function_tool.needs_approval, context, params, call_id, strict=strict
    )
    result.check_invocation(function_tool, arguments)
    result.outcome = "require_approval" if needs_approval else "invoke"
    if needs_approval:
        result.prepared = None
    return result
