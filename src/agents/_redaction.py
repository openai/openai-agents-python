"""Per-invocation redaction at Runner payload and persistence boundaries."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import fields, is_dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeVar, cast

from pydantic import BaseModel

from . import _debug
from .exceptions import (
    BaseExceptionGroup,
    ModelBehaviorError,
    UserError,
    _is_error_data_redacted,
    _mark_error_data_redacted,
    _prepare_data_redacted_error,
    _raise_data_redacted_error,
    _safe_data_redacted_error,
)

if TYPE_CHECKING:
    from .agent import Agent
    from .items import ModelResponse
    from .run_config import ModelInputData
    from .tool import FunctionTool

_T = TypeVar("_T")
_Redactor = Callable[[str], str]
_Mode = Literal["payload", "item", "check"]
_redactor: ContextVar[_Redactor | None] = ContextVar("agents_redact_text", default=None)


def current_redactor() -> _Redactor | None:
    return _redactor.get()


def raise_protected_error(error: BaseException) -> NoReturn:
    """Remove diagnostics while preserving grouped interruptions in a protected run."""
    if issubclass(type(error), BaseExceptionGroup):
        _raise_data_redacted_error(_safe_data_redacted_error(error))
    _raise_data_redacted_error(_prepare_data_redacted_error(error))


@contextmanager
def redaction_scope(redactor: _Redactor | None) -> Iterator[None]:
    token = _redactor.set(redactor)
    try:
        yield
    except (asyncio.CancelledError, BaseExceptionGroup) as error:
        # Task can replace the sanitized cancellation on Python 3.10.
        if redactor is None or _is_error_data_redacted(error):
            raise
        raise_protected_error(error)
    finally:
        _redactor.reset(token)


@contextmanager
def hook_payload_diagnostics(
    message: str,
    *,
    redact_by_default: bool = True,
    error_type: type[UserError | ModelBehaviorError] = UserError,
) -> Iterator[None]:
    """Keep rejected SDK payloads out of public diagnostics when tool data is redacted."""
    try:
        yield
        return
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit) as interrupted:
        if current_redactor() is None:
            raise
        _raise_data_redacted_error(_prepare_data_redacted_error(interrupted))
    except Exception as error:
        if current_redactor() is not None and _is_error_data_redacted(error):
            raise
        if current_redactor() is None and not (redact_by_default and _debug.DONT_LOG_TOOL_DATA):
            raise
    except BaseException as error:
        if current_redactor() is None:
            raise
        _raise_data_redacted_error(_safe_data_redacted_error(error))
    safe_error = error_type(message)
    _mark_error_data_redacted(safe_error)
    _raise_data_redacted_error(safe_error)


def _text(value: str, redact: _Redactor, *, check: bool) -> str:
    result = redact(value)
    if type(result) is not str:
        if inspect.iscoroutine(result):
            result.close()
        raise ValueError
    if result == value:
        return value
    if check:
        raise ValueError
    _text(result, redact, check=True)
    return result


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        # Different decoders can disagree about which repeated value to keep.
        raise ValueError
    return result


def _decode_json(value: str) -> Any:
    if value.lstrip().startswith(("{", "[", '"')):
        try:
            return json.loads(value, object_pairs_hook=_unique_keys)
        except json.JSONDecodeError:
            pass
    return value


def _entries(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return {**vars(value), **(value.model_extra or {})}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, dict):
        return value
    # An arbitrary __str__ or serializer could expose uninspected content.
    raise ValueError


def _text_parts(value: Any, field: str = "") -> Iterator[tuple[str | None, str]]:
    if isinstance(value, str):
        decoded = _decode_json(value)
        if decoded is value:
            yield field, value
        else:
            yield from _text_parts(decoded, field)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _text_parts(item, field)
    elif value is not None and not isinstance(value, (int, float, bool)):
        for key, item in _entries(value).items():
            yield None, key
            # Text parts, messages and refusals can all contribute to the visible text.
            name = "" if key in ("text", "content", "refusal") else key
            yield from _text_parts(item, name)


def _check_joined(value: Any, redact: _Redactor) -> None:
    # Check ordered values, keys and individual columns of decoded records.
    fields: dict[str | None, list[str]] = {}
    ordered = []
    for field, text in _text_parts(value):
        fields.setdefault(field, []).append(text)
        if field is not None:
            ordered.append(text)
    if len(ordered) > 1:
        _walk("".join(ordered), redact, "check")
    for parts in fields.values():
        if len(parts) > 1 and parts != ordered:
            _walk("".join(parts), redact, "check")


def _walk(value: Any, redact: _Redactor, mode: _Mode) -> Any:
    if isinstance(value, str):
        # Function arguments and serialized RunState can contain escaped JSON strings.
        decoded = _decode_json(value)
        if decoded is not value:
            clean = _walk(decoded, redact, "check" if mode == "check" else "payload")
            if clean is not decoded:
                value = json.dumps(clean, ensure_ascii=False, allow_nan=False)
            return _text(value, redact, check=True)
        return _text(value, redact, check=mode == "check")
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        items = [_walk(item, redact, mode) for item in value]
        # Structured prompts and tool results can split a secret across text parts.
        if len(items) > 1:
            _check_joined(items, redact)
        if all(a is b for a, b in zip(items, value, strict=True)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    entries = _entries(value)

    if mode == "item" and entries.get("type") == "mcp_approval_request":
        # The decision refers to server-held arguments. Never display an edited action.
        mode = "check"
    changes = {}
    for key, item in entries.items():
        if not isinstance(key, str):
            raise ValueError
        _text(key, redact, check=True)
        child = mode
        if mode == "item":
            # Keep protocol identifiers, routing and opaque data intact.
            if key in ("content", "summary", "output", "codex_reasoning_summary"):
                child = "item"
            elif key in ("text", "refusal", "arguments", "input"):
                child = "payload"
            else:
                child = "check"
        clean = _walk(item, redact, child)
        if isinstance(clean, str):
            _text(key + clean, redact, check=True)
        if clean is not item:
            changes[key] = clean
    if len(entries) > 1:
        _text("".join(entries), redact, check=True)
        _check_joined({**entries, **changes} if changes else entries, redact)
    if not changes:
        return value
    if isinstance(value, BaseModel):
        return value.model_copy(update=changes)
    if isinstance(value, dict):
        return {**value, **changes}
    return replace(value, **changes)


def _apply(value: _T, mode: _Mode) -> _T:
    redact = current_redactor()
    if redact is None:
        return value
    try:
        return cast(_T, _walk(value, redact, mode))
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit) as interrupted:
        _raise_data_redacted_error(_prepare_data_redacted_error(interrupted))
    except Exception:
        # Neither callback exceptions nor validation errors are safe diagnostics.
        pass
    except BaseException as failure:
        _raise_data_redacted_error(_safe_data_redacted_error(failure))

    error = UserError("RunConfig.redact_text could not safely redact this data.")
    _mark_error_data_redacted(error)
    _raise_data_redacted_error(error)


def require_clean(value: object) -> None:
    """Refuse a change to an identity, approval receipt or serialized checkpoint."""
    _apply(value, "check")


def require_clean_history(items: list[Any]) -> None:
    """Check the transcript, including the text a consumer sees without protocol fields."""
    require_clean(items)
    messages = []
    visible = []
    for item in items:
        if not isinstance(item, (dict, BaseModel)):
            continue
        fields = _entries(item)
        if fields.get("role") == "assistant":
            content = fields.get("content")
            messages.append(content)
            visible.append(content)
        elif "output" in fields:
            visible.append(fields["output"])
    if len(messages) > 1:
        require_clean(messages)
    if len(visible) > len(messages):
        require_clean(visible)


def redact_payload(value: _T) -> _T:
    return _apply(value, "payload")


def redact_items(value: _T) -> _T:
    return _apply(value, "item")


def require_clean_function(tool: FunctionTool) -> None:
    if current_redactor() is not None:
        require_clean(
            (
                tool.name,
                tool.description,
                tool.params_json_schema,
                tool._tool_namespace,
                tool._tool_namespace_description,
                tool._mcp_title,
                tool._mcp_annotations,
                tool._mcp_tool_binding,
            )
        )


def require_clean_agent(agent: Agent[Any]) -> None:
    if current_redactor() is None:
        return
    from ._run_state_agent_identity import _iter_agent_graph
    from .tool import FunctionTool

    # Events reference the public agents. Don't mutate shared configuration to hide secrets.
    for target in _iter_agent_graph(agent):
        require_clean((target.name, target.handoff_description))
        if isinstance(target.instructions, str):
            require_clean(target.instructions)
        if not callable(target.prompt):
            require_clean(target.prompt)
        for tool in target.tools:
            if isinstance(tool, FunctionTool):
                require_clean_function(tool)


def redact_model_input(data: ModelInputData, prompt: object) -> None:
    if current_redactor() is not None:
        # Check server references and tracked history without rewriting them.
        require_clean((prompt, data.input))
        data.instructions = redact_payload(data.instructions)


def redact_response(response: ModelResponse, history: list[Any]) -> ModelResponse:
    if current_redactor() is None:
        return response
    from .items import ItemHelpers
    from .models._codex_items import CodexPendingCustomCall, CodexPendingFunctionCall

    require_clean((response.response_id, response.request_id, response.raw_usage))
    for item in response.output:
        if isinstance(item, (CodexPendingCustomCall, CodexPendingFunctionCall)):
            # A human must approve the exact action that Core will execute.
            require_clean(item)
    result = copy(response)
    result.output = redact_items(response.output)
    require_clean_history([*history, *result.output])
    # Consumers concatenate text parts. Refuse fragments which form a secret together.
    for extract in (ItemHelpers.extract_text, ItemHelpers.extract_refusal):
        require_clean("".join(extract(item) or "" for item in result.output))
    return result
