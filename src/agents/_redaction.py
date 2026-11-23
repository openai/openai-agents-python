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
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from pydantic import BaseModel

from . import _debug
from .exceptions import (
    ModelBehaviorError,
    UserError,
    _is_error_data_redacted,
    _mark_error_data_redacted,
    _prepare_data_redacted_error,
    _raise_data_redacted_error,
)

if TYPE_CHECKING:
    from .agent import Agent
    from .items import ModelResponse
    from .run_config import ModelInputData

_T = TypeVar("_T")
_Redactor = Callable[[str], str]
_Mode = Literal["payload", "item", "check"]
_redactor: ContextVar[_Redactor | None] = ContextVar("agents_redact_text", default=None)


def current_redactor() -> _Redactor | None:
    return _redactor.get()


@contextmanager
def redaction_scope(redactor: _Redactor | None) -> Iterator[None]:
    token = _redactor.set(redactor)
    try:
        yield
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


def _walk(value: Any, redact: _Redactor, mode: _Mode) -> Any:
    if isinstance(value, str):
        # Function arguments and serialized RunState can contain escaped JSON strings.
        if value.lstrip().startswith(("{", "[", '"')):
            try:
                decoded = json.loads(value, object_pairs_hook=_unique_keys)
            except json.JSONDecodeError:
                pass
            else:
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
            parts = [
                item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                for item in items
            ]
            texts = [part for part in parts if isinstance(part, str)]
            if len(texts) > 1:
                _walk("".join(texts), redact, "check")
        if all(a is b for a, b in zip(items, value, strict=True)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, BaseModel):
        entries = {**vars(value), **(value.model_extra or {})}
    elif is_dataclass(value) and not isinstance(value, type):
        entries = {field.name: getattr(value, field.name) for field in fields(value)}
    elif isinstance(value, dict):
        entries = value
    else:
        # An arbitrary __str__ or serializer could expose uninspected content.
        raise ValueError

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
        if clean is not item:
            changes[key] = clean
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

    error = UserError("RunConfig.redact_text could not safely redact this data.")
    _mark_error_data_redacted(error)
    _raise_data_redacted_error(error)


def require_clean(value: object) -> None:
    """Refuse a change to an identity, approval receipt or serialized checkpoint."""
    _apply(value, "check")


def redact_payload(value: _T) -> _T:
    return _apply(value, "payload")


def redact_items(value: _T) -> _T:
    return _apply(value, "item")


def require_clean_agent(agent: Agent[Any]) -> None:
    if current_redactor() is None:
        return
    from ._run_state_agent_identity import _iter_agent_graph

    # Events reference the public agents. Don't mutate shared configuration to hide secrets.
    for target in _iter_agent_graph(agent):
        require_clean((target.name, target.handoff_description))
        if isinstance(target.instructions, str):
            require_clean(target.instructions)
        if not callable(target.prompt):
            require_clean(target.prompt)


def redact_model_input(data: ModelInputData, prompt: object) -> None:
    if current_redactor() is not None:
        # Check server references and tracked history without rewriting them.
        require_clean((prompt, data.input))
        data.instructions = redact_payload(data.instructions)


def redact_response(response: ModelResponse) -> ModelResponse:
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
    # Consumers concatenate text parts. Refuse fragments which form a secret together.
    for extract in (ItemHelpers.extract_text, ItemHelpers.extract_refusal):
        require_clean("".join(extract(item) or "" for item in result.output))
    return result
