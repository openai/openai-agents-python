import random

from mcp.server.fastmcp import FastMCP

# Create server
mcp = FastMCP("Echo Server")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers"""
    print(f"[debug-server] add({a}, {b})")
    return a + b


@mcp.tool()
def get_secret_word() -> str:
    print("[debug-server] get_secret_word()")
    return random.choice(["apple", "banana", "cherry"])


@mcp.tool()
def get_current_weather(city: str) -> str:
    print(f"[debug-server] get_current_weather({city})")
    # Keep tool output deterministic so this example is stable in CI and offline environments.
    weather_by_city = {
        "tokyo": "sunny with a light breeze and 20°C",
        "san francisco": "cool and foggy with 14°C",
        "new york": "partly cloudy with 18°C",
    }
    forecast = weather_by_city.get(city.strip().lower())
    if forecast:
        return f"The weather in {city} is {forecast}."
    return f"The weather data for {city} is unavailable in this demo."


if __name__ == "__main__":
    mcp.run(transport="sse")
