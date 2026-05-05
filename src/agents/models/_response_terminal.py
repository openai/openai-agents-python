from __future__ import annotations

from typing import Any

from openai.types.responses import Response

from ..exceptions import ModelBehaviorError, _mark_error_to_drain_stream_events


def format_response_terminal_failure(
    event_type: str,
    response: Response | None,
) -> str:
    message = f"Responses stream ended with terminal event `{event_type}`."
    if response is None:
        return message

    details: list[str] = []
    status = getattr(response, "status", None)
    if status:
        details.append(f"status={status}")
    error = getattr(response, "error", None)
    if error:
        details.append(f"error={error}")
    incomplete_details = getattr(response, "incomplete_details", None)
    if incomplete_details:
        details.append(f"incomplete_details={incomplete_details}")

    if details:
        message = f"{message} {'; '.join(details)}."
    return message


def format_response_error_event(event_type: str, event: Any) -> str:
    message = f"Responses stream ended with terminal event `{event_type}`."
    details: list[str] = []
    code = getattr(event, "code", None)
    if code:
        details.append(f"code={code}")
    error_message = getattr(event, "message", None)
    if error_message:
        details.append(f"message={error_message}")
    param = getattr(event, "param", None)
    if param:
        details.append(f"param={param}")

    if details:
        message = f"{message} {'; '.join(details)}."
    return message


def response_terminal_failure_error(
    event_type: str,
    response: Response | None,
) -> ModelBehaviorError:
    error = ModelBehaviorError(format_response_terminal_failure(event_type, response))
    _mark_error_to_drain_stream_events(error)
    return error


def response_error_event_failure_error(event_type: str, event: Any) -> ModelBehaviorError:
    error = ModelBehaviorError(format_response_error_event(event_type, event))
    _mark_error_to_drain_stream_events(error)
    return error
