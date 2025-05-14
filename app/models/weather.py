from enum import auto
from typing import Optional

from pydantic import BaseModel
from strenum import StrEnum


class City(StrEnum):
    """Enum of cities supported by the weather agent."""

    NEW_YORK = auto()
    LONDON = auto()
    TOKYO = auto()
    SYDNEY = auto()

    @classmethod
    def get_display_name(cls, city: "City") -> str:
        """Get the display name for a city."""
        display_names = {
            cls.NEW_YORK: "New York",
            cls.LONDON: "London",
            cls.TOKYO: "Tokyo",
            cls.SYDNEY: "Sydney",
        }
        return display_names.get(city, str(city))


class WeatherData(BaseModel):
    """Weather data model."""

    city: str
    temperature_range: str
    conditions: str

    @classmethod
    def from_city_enum(cls, city: City) -> "WeatherData":
        """Create a WeatherData instance from a City enum."""
        # Mock weather data for each city
        weather_data = {
            City.NEW_YORK: cls(
                city=City.get_display_name(City.NEW_YORK),
                temperature_range="15-25°C",
                conditions="Partly cloudy",
            ),
            City.LONDON: cls(
                city=City.get_display_name(City.LONDON),
                temperature_range="10-15°C",
                conditions="Rainy",
            ),
            City.TOKYO: cls(
                city=City.get_display_name(City.TOKYO),
                temperature_range="20-30°C",
                conditions="Sunny",
            ),
            City.SYDNEY: cls(
                city=City.get_display_name(City.SYDNEY),
                temperature_range="18-23°C",
                conditions="Clear",
            ),
        }

        return weather_data.get(
            city,
            cls(
                city=(
                    City.get_display_name(city) if isinstance(city, City) else str(city)
                ),
                temperature_range="15-25°C",
                conditions="Unknown",
            ),
        )


class WeatherRequest(BaseModel):
    query: str
    stream: bool = False
    city: Optional[City] = None

    class Config:
        use_enum_values = True


class WeatherResponse(BaseModel):
    response: str
    weather_data: Optional[WeatherData] = None
    trace_id: Optional[str] = None
