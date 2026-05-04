from __future__ import annotations

from openai.types.responses import Response


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
