"""Weather tool - demonstrates returning structured data (dict) as tool result."""

from typing import Any

from agents import function_tool

# Mock data used for teaching project to avoid external dependencies
_MOCK_WEATHER = {
    "beijing": {"city": "Beijing", "temp_c": 22, "condition": "Sunny", "humidity": 35},
    "shanghai": {"city": "Shanghai", "temp_c": 26, "condition": "Cloudy", "humidity": 70},
    "guangzhou": {"city": "Guangzhou", "temp_c": 30, "condition": "Thunderstorms", "humidity": 85},
    "shenzhen": {"city": "Shenzhen", "temp_c": 29, "condition": "Cloudy", "humidity": 78},
    "tokyo": {"city": "Tokyo", "temp_c": 18, "condition": "Overcast", "humidity": 60},
    "london": {"city": "London", "temp_c": 12, "condition": "Light Rain", "humidity": 88},
    "new york": {"city": "New York", "temp_c": 15, "condition": "Sunny", "humidity": 50},
}


@function_tool
def get_weather(city: str) -> dict[str, Any]:
    """Query the current weather for a specific city. Only supports cities in the mock dictionary.

    Args:
        city: City english name (lowercase) or Chinese name, e.g., 'beijing' / '北京'.
    """
    key = city.strip().lower()
    # Map Chinese names directly to keys
    cn_to_en = {"北京": "beijing", "上海": "shanghai", "广州": "guangzhou", "深圳": "shenzhen"}
    key = cn_to_en.get(key, key)
    if key in _MOCK_WEATHER:
        return _MOCK_WEATHER[key]
    return {
        "error": f"Weather data for {city} not found (mock mode only supports: beijing/shanghai/guangzhou/shenzhen/tokyo/london/new york)"
    }


WEATHER_TOOLS = [get_weather]
