"""Realtime weather voice agent configuration."""

from __future__ import annotations

from typing import Any, Final

from agents import function_tool
from agents.realtime import RealtimeAgent, RealtimeRunConfig

FORECASTS: Final[dict[str, dict[str, Any]]] = {
    "new york": {
        "name": "New York City",
        "condition": "sunny with a light coastal breeze",
        "high_f": 78,
        "low_f": 65,
        "tip": "A light jacket is handy for evening walks along the Hudson River.",
    },
    "san francisco": {
        "name": "San Francisco",
        "condition": "morning fog that burns off into a clear afternoon",
        "high_f": 68,
        "low_f": 56,
        "tip": "Carry layers because neighborhoods like the Mission stay warmer than the Presidio.",
    },
    "seattle": {
        "name": "Seattle",
        "condition": "scattered clouds and a gentle drizzle",
        "high_f": 64,
        "low_f": 52,
        "tip": "Waterproof shoes make downtown strolls more comfortable when sidewalks are damp.",
    },
    "austin": {
        "name": "Austin",
        "condition": "sunny skies with a late afternoon breeze",
        "high_f": 92,
        "low_f": 74,
        "tip": "Stay hydrated and find shade during outdoor concerts.",
    },
}


def _format_forecast(city: str, details: dict[str, Any]) -> str:
    """Create a friendly weather summary from forecast details."""
    return (
        f"In {details['name']}, expect {details['condition']}. "
        f"Temperatures range from about {details['low_f']}°F overnight to {details['high_f']}°F during the day. "
        f"Tip: {details['tip']}"
    )


@function_tool
def lookup_weather(city: str) -> str:
    """Return a mock weather report for the requested city."""
    normalized_city = city.strip().lower()
    forecast = FORECASTS.get(normalized_city)
    if forecast is None:
        return (
            "I do not have live data for that city, but typical weather is mild with occasional clouds. "
            "Try asking about New York, San Francisco, Seattle, or Austin for a more specific report."
        )

    return _format_forecast(city=normalized_city, details=forecast)


REALTIME_RUN_CONFIG: Final[RealtimeRunConfig] = {
    "model_settings": {
        "model_name": "gpt-4o-realtime-preview",
        "voice": "alloy",
        "modalities": ["text", "audio"],
        "input_audio_transcription": {"model": "whisper-1"},
    }
}


def create_weather_agent() -> RealtimeAgent[None]:
    """Create the realtime agent used by the Streamlit demo."""
    return RealtimeAgent(
        name="Weather Buddy",
        instructions=(
            "You are a cheerful weather assistant that speaks naturally in short sentences. "
            "When someone asks about the forecast you must call the `lookup_weather` tool before responding. "
            "Answer with a friendly summary that is easy to hear aloud."
        ),
        tools=[lookup_weather],
    )
