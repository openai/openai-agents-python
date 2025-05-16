import json
import logging.config

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import weather_agent

# Set up logging
with open("app/config/logging_config.json", "r") as config_file:
    logging_config = json.load(config_file)

logging.config.dictConfig(logging_config)
logger = logging.getLogger(__name__)

# Create the FastAPI app
app = FastAPI(
    title="Weather API with GPT-4.1",
    description="An API that uses GPT-4.1 to provide weather information for cities",
    version="1.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*"
    ],  # For development. In production, specify the allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(weather_agent.router)

@app.get("/")
async def root():
    """
    Root endpoint that returns a simple welcome message.

    Returns:
        A welcome message
    """
    return {"message": "Welcome to the Weather API with GPT-4.1"}


@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        Health status
    """
    return {"status": "ok"}
