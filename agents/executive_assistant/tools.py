from __future__ import annotations

from agents import function_tool


@function_tool
def get_calendar_events(date: str) -> str:
    """Retrieve calendar events for a given date."""
    # TODO: Integrate with calendar API.
    return f"No events found for {date}."


@function_tool
def send_email(recipient: str, subject: str, body: str) -> str:
    """Send a simple email."""
    # TODO: Integrate with email service.
    return "Email sent."
