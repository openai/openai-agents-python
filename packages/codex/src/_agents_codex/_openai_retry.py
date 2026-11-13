"""OpenAI adapter advice; Core retains final replay authority."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

from ._model_retry import _normalized_error
from .models import ModelRetryAdviceRequest
from .retry import RetryAdvice


def _read(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except BaseException:
        return None


def _header(error: BaseException, name: str) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for headers in (
            _read(_read(current, "response"), "headers"),
            _read(current, "headers"),
            _read(current, "response_headers"),
        ):
            if isinstance(headers, Mapping):
                try:
                    for key, value in headers.items():
                        if str(key).lower() == name and isinstance(value, str):
                            return value
                except BaseException:
                    pass
        following = _read(current, "__cause__") or _read(current, "__context__")
        current = following if isinstance(following, BaseException) else None
    return None


def openai_retry_advice(request: ModelRetryAdviceRequest) -> RetryAdvice | None:
    error = request.error
    normalized = _normalized_error(error)
    reason = normalized.message
    if _read(error, "unsafe_to_replay") is True or (
        "the request may have been accepted, so the sdk will not automatically "
        "retry this websocket request." in reason.lower()
    ):
        return RetryAdvice(suggested=False, replay_safety="unsafe", reason=reason)
    delay = normalized.retry_after_ms
    if delay is None:
        for name, scale in (("retry-after-ms", 1.0), ("retry-after", 1000.0)):
            raw = _header(error, name)
            if raw is None:
                continue
            try:
                candidate = float(raw) * scale
            except (ValueError, TypeError, OverflowError):
                if name != "retry-after":
                    continue
                try:
                    candidate = max(
                        0.0, (parsedate_to_datetime(raw).timestamp() - time.time()) * 1000
                    )
                except (ValueError, TypeError, OverflowError):
                    continue
            if math.isfinite(candidate) and candidate >= 0:
                delay = candidate
                break
    hint = _header(error, "x-should-retry")
    hint = hint.strip().lower() if hint is not None else None
    if hint == "true":
        return RetryAdvice(
            suggested=True, replay_safety="safe", retry_after_ms=delay, reason=reason
        )
    if hint == "false":
        return RetryAdvice(suggested=False, retry_after_ms=delay, reason=reason)
    if normalized.is_network_error or normalized.is_timeout:
        return RetryAdvice(suggested=True, retry_after_ms=delay, reason=reason)
    status = normalized.status_code
    if status in (408, 409, 429) or (status is not None and status >= 500):
        stateful = bool(
            request.request.body.get("previous_response_id")
            or request.request.body.get("conversation")
        )
        return RetryAdvice(
            suggested=True,
            retry_after_ms=delay,
            replay_safety="safe" if stateful else "unknown",
            reason=reason,
        )
    return RetryAdvice(retry_after_ms=delay, reason=reason) if delay is not None else None
