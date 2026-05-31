from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from openai.types.responses import Response

from agents.exceptions import ModelBehaviorError
from agents.models._response_terminal import (
    format_response_error_event,
    format_response_terminal_failure,
    response_error_event_failure_error,
    response_terminal_failure_error,
)


def test_format_response_terminal_failure_without_response() -> None:
    message = format_response_terminal_failure("response.failed", None)

    assert message == "Responses stream ended with terminal event `response.failed`."


def test_format_response_terminal_failure_includes_all_details() -> None:
    response = SimpleNamespace(
        status="incomplete",
        error="boom",
        incomplete_details="max_output_tokens",
    )

    message = format_response_terminal_failure("response.incomplete", cast(Response, response))

    assert message == (
        "Responses stream ended with terminal event `response.incomplete`. "
        "status=incomplete; error=boom; incomplete_details=max_output_tokens."
    )


def test_format_response_terminal_failure_omits_falsy_details() -> None:
    response = SimpleNamespace(status=None, error="boom", incomplete_details=None)

    message = format_response_terminal_failure("response.failed", cast(Response, response))

    assert message == "Responses stream ended with terminal event `response.failed`. error=boom."


def test_format_response_terminal_failure_with_no_details_keeps_base_message() -> None:
    response = SimpleNamespace(status=None, error=None, incomplete_details=None)

    message = format_response_terminal_failure("response.failed", cast(Response, response))

    assert message == "Responses stream ended with terminal event `response.failed`."


def test_format_response_error_event_includes_all_details() -> None:
    event = SimpleNamespace(code="rate_limit", message="slow down", param="model")

    message = format_response_error_event("error", event)

    assert message == (
        "Responses stream ended with terminal event `error`. "
        "code=rate_limit; message=slow down; param=model."
    )


def test_format_response_error_event_omits_falsy_details() -> None:
    event = SimpleNamespace(code=None, message="slow down", param=None)

    message = format_response_error_event("error", event)

    assert message == "Responses stream ended with terminal event `error`. message=slow down."


def test_format_response_error_event_with_no_details_keeps_base_message() -> None:
    event = SimpleNamespace(code=None, message=None, param=None)

    message = format_response_error_event("error", event)

    assert message == "Responses stream ended with terminal event `error`."


def test_response_terminal_failure_error_wraps_formatted_message() -> None:
    response = SimpleNamespace(status="failed", error=None, incomplete_details=None)

    error = response_terminal_failure_error("response.failed", cast(Response, response))

    assert isinstance(error, ModelBehaviorError)
    assert str(error) == (
        "Responses stream ended with terminal event `response.failed`. status=failed."
    )


def test_response_error_event_failure_error_wraps_formatted_message() -> None:
    event = SimpleNamespace(code="rate_limit", message=None, param=None)

    error = response_error_event_failure_error("error", event)

    assert isinstance(error, ModelBehaviorError)
    assert str(error) == ("Responses stream ended with terminal event `error`. code=rate_limit.")
