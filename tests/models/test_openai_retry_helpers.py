"""Unit tests for the low-level helpers in :mod:`agents.models._openai_retry`.

These exercise the header-parsing, status-extraction, and error-code helpers
directly, plus a few public ``get_openai_retry_advice`` branches that the broader
behavioral suite in ``test_model_retry.py`` does not reach.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from agents.models._openai_retry import (
    _get_error_code,
    _get_header_value,
    _get_status_code,
    _header_lookup,
    _parse_retry_after,
    _parse_retry_after_ms,
    get_openai_retry_advice,
)
from agents.retry import ModelRetryAdviceRequest


class _HeaderError(Exception):
    """Error that exposes headers through a plain attribute rather than a response."""

    def __init__(self, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        if headers is not None:
            self.headers = headers


def _make_request(error: Exception, **kwargs: object) -> ModelRetryAdviceRequest:
    return ModelRetryAdviceRequest(error=error, attempt=1, stream=False, **kwargs)  # type: ignore[arg-type]


def test_header_lookup_plain_mapping_matches_case_insensitively() -> None:
    headers = {"Retry-After": "5", "X-Other": "ignored"}
    assert _header_lookup(headers, "retry-after") == "5"
    assert _header_lookup(headers, "missing") is None


def test_header_lookup_httpx_headers() -> None:
    headers = httpx.Headers({"retry-after": "7"})
    assert _header_lookup(headers, "retry-after") == "7"
    assert _header_lookup(None, "retry-after") is None


def test_get_header_value_reads_response_headers_attr() -> None:
    class _Err(Exception):
        response_headers = {"retry-after": "3"}

    assert _get_header_value(_Err("boom"), "retry-after") == "3"


def test_parse_retry_after_ms_invalid_returns_none() -> None:
    assert _parse_retry_after_ms(None) is None
    assert _parse_retry_after_ms("not-a-number") is None
    assert _parse_retry_after_ms("-100") is None
    assert _parse_retry_after_ms("1500") == 1.5


def test_parse_retry_after_numeric_and_http_date() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("2") == 2.0
    assert _parse_retry_after("-1") is None

    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    parsed = _parse_retry_after(format_datetime(future))
    assert parsed is not None and parsed > 0

    assert _parse_retry_after("definitely not a date") is None


def test_get_status_code_from_status_code_and_status_attrs() -> None:
    class _StatusCode(Exception):
        status_code = 503

    class _Status(Exception):
        status = 504

    assert _get_status_code(_StatusCode("a")) == 503
    assert _get_status_code(_Status("b")) == 504
    assert _get_status_code(Exception("none")) is None


def test_get_error_code_from_body_mapping() -> None:
    class _NestedBody(Exception):
        body = {"error": {"code": "rate_limit_exceeded"}}

    class _TopLevelBody(Exception):
        body = {"code": "server_error"}

    assert _get_error_code(_NestedBody("a")) == "rate_limit_exceeded"
    assert _get_error_code(_TopLevelBody("b")) == "server_error"
    assert _get_error_code(Exception("none")) is None


def test_advice_unsafe_to_replay() -> None:
    error = Exception("cannot replay")
    error.unsafe_to_replay = True  # type: ignore[attr-defined]

    advice = get_openai_retry_advice(_make_request(error))

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"


def test_advice_websocket_request_is_unsafe() -> None:
    message = (
        "The request may have been accepted, so the SDK will not automatically "
        "retry this websocket request."
    )
    advice = get_openai_retry_advice(_make_request(Exception(message)))

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"


def test_advice_respects_x_should_retry_false() -> None:
    error = _HeaderError("nope", headers={"x-should-retry": "false"})

    advice = get_openai_retry_advice(_make_request(error))

    assert advice is not None
    assert advice.suggested is False


def test_advice_returns_retry_after_only_when_no_other_signal() -> None:
    # A 400 with no x-should-retry header and no network/timeout signal would not
    # normally retry, but a retry-after header still yields advice carrying the delay.
    error = _HeaderError("slow down", headers={"retry-after": "2"})

    advice = get_openai_retry_advice(_make_request(error))

    assert advice is not None
    assert advice.retry_after == 2.0
    # This branch only conveys the server-provided delay; it does not assert a
    # retry decision, so ``suggested`` keeps its unset default.
    assert advice.suggested is None
