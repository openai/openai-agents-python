"""Weather agent router with OpenAI Agent integration."""

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from typing import Dict, Any, Optional
import logging
import asyncio
import uuid

from pydantic import BaseModel
from agents import Agent, Runner, function_tool

from app.models.weather import WeatherData, WeatherRequest, WeatherResponse, City

# Set up logging
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/weather",
    tags=["weather"],
    responses={404: {"description": "Not found"}},
)


# Define the function tool to get weather data
@function_tool
def get_weather(city: City) -> Dict[str, Any]:
    """
    Get the current weather for a city.

    Args:
        city: The city to get weather for (NEW_YORK, LONDON, TOKYO, SYDNEY)

    Returns:
        A dictionary with the weather information.
    """
    weather_data = WeatherData.from_city_enum(city)
    return {
        "city": weather_data.city,
        "temperature_range": weather_data.temperature_range,
        "conditions": weather_data.conditions,
    }


# Create the agent with GPT-4.1
weather_agent = Agent(
    name="weather_assistant",
    model="gpt-4o",
    tools=[get_weather],
    instructions="""
    You are a helpful weather assistant that can provide current weather information for cities.
    You can tell users about the weather, temperature range, and conditions for the following cities:
    - New York
    - London
    - Tokyo
    - Sydney
    
    When a user asks about the weather in a supported city, use the get_weather tool to retrieve the information.
    If the user asks about a city that is not supported, politely inform them that you only have data for the supported cities.
    Always respond in a friendly and conversational manner.
    """,
)


@router.post("/ask", response_model=WeatherResponse)
async def ask_weather_agent(request: WeatherRequest) -> WeatherResponse:
    """
    Ask the weather agent about weather information.

    Args:
        request: The weather request including the query and optional city

    Returns:
        A response with weather data and the agent's response
    """
    logger.info(f"Received weather query: {request.query}")

    # Create a trace ID for tracking
    trace_id = str(uuid.uuid4())
    logger.debug(f"Generated trace ID: {trace_id}")

    try:
        # Run the agent with the query using the Runner class method
        result = await Runner.run(starting_agent=weather_agent, input=request.query)

        # Log the result structure for debugging
        logger.debug(f"Result type: {type(result)}")
        logger.debug(f"Result attributes: {dir(result)}")
        logger.debug(
            f"New items count: {len(result.new_items) if hasattr(result, 'new_items') else 'no new_items attribute'}"
        )

        if hasattr(result, "new_items") and result.new_items:
            for i, item in enumerate(result.new_items):
                logger.debug(f"Item {i} type: {type(item)}")
                logger.debug(f"Item {i} attributes: {dir(item)}")
                if hasattr(item, "raw_item"):
                    logger.debug(f"Item {i} raw_item: {item.raw_item}")
                    if hasattr(item.raw_item, "content"):
                        logger.debug(
                            f"Item {i} raw_item.content: {item.raw_item.content}"
                        )

        # Extract the last message from the result
        response_text = "No response generated"
        if hasattr(result, "new_items") and result.new_items:
            for item in reversed(result.new_items):
                if hasattr(item, "raw_item") and hasattr(item.raw_item, "content"):
                    for content_item in item.raw_item.content:
                        if hasattr(content_item, "text"):
                            response_text = content_item.text
                            break

        # Extract the weather data if a city was specified
        weather_data = None
        if request.city:
            weather_data = WeatherData.from_city_enum(request.city)

        # Return the response
        return WeatherResponse(
            response=response_text, weather_data=weather_data, trace_id=trace_id
        )
    except Exception as e:
        logger.error(f"Error processing weather query: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error processing request: {str(e)}"
        )


@router.post("/stream", response_model=WeatherResponse)
async def stream_weather_agent(
    background_tasks: BackgroundTasks, request: WeatherRequest
) -> WeatherResponse:
    """
    Stream the response from the weather agent.

    Args:
        background_tasks: FastAPI background tasks
        request: The weather request

    Returns:
        A streaming response from the weather agent
    """
    if not request.stream:
        return await ask_weather_agent(request)

    trace_id = str(uuid.uuid4())
    logger.debug(f"Generated trace ID for streaming: {trace_id}")

    try:
        # Set up streaming using the Runner.run_streamed class method
        background_tasks.add_task(
            Runner.run_streamed, starting_agent=weather_agent, input=request.query
        )

        # Get initial weather data if a city is specified
        weather_data = None
        if request.city:
            weather_data = WeatherData.from_city_enum(request.city)

        # Return initial response
        return WeatherResponse(
            response="Starting to process your weather query...",
            weather_data=weather_data,
            trace_id=trace_id,
        )
    except Exception as e:
        logger.error(f"Error setting up streaming: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error setting up streaming: {str(e)}"
        )


@router.get("/cities", response_model=Dict[str, str])
async def list_cities() -> Dict[str, str]:
    """
    List all available cities and their display names.

    Returns:
        A dictionary mapping city keys to their display names
    """
    return {city.name: City.get_display_name(city) for city in City}


@router.get("/{city}", response_model=WeatherData)
async def get_city_weather(city: City) -> WeatherData:
    """
    Get weather data for a specific city.

    Args:
        city: The city to get weather for

    Returns:
        Weather data for the specified city
    """
    try:
        return WeatherData.from_city_enum(city)
    except Exception as e:
        logger.error(f"Error retrieving weather for {city}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error retrieving weather data: {str(e)}"
        )
