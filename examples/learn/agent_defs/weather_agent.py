"""Weather Agent - Holds the weather tool."""

from config import MODEL  # type: ignore[import-not-found]
from tools.weather import WEATHER_TOOLS  # type: ignore[import-not-found]

from agents import Agent

weather_agent = Agent(
    name="WeatherAgent",
    instructions=(
        "You are a weather assistant. When the user asks about the weather in a city, call the get_weather tool, "
        "then give a one-sentence reply about the weather (temperature, condition, humidity). "
        "If the tool returns an error field, tell the error exactly as is to the user."
    ),
    model=MODEL,
    tools=WEATHER_TOOLS,
)
