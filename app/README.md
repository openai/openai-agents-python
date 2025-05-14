# OpenAI Agents API

This project provides a FastAPI application that exposes OpenAI agents through a RESTful API.

## Features

- FastAPI-based RESTful API for OpenAI agents
- Development server with hot-reloading

## Requirements

- Python 3.8+
- Required packages are listed in `requirements.txt`

## Setup

1. Run the development server:

```bash
./dev_appserver.py --reload
```

This will start the server at http://127.0.0.1:8000

## API Endpoints

### Weather Agent

- `GET /weather/cities`: Get a list of available cities
  - Returns: `{"NEW_YORK": "New York", "LONDON": "London", "TOKYO": "Tokyo", "SYDNEY": "Sydney"}`

- `GET /weather/{city}`: Get weather data for a specific city
  - Example: `GET /weather/NEW_YORK`
  - Returns: `{"city": "New York", "temperature_range": "15-25°C", "conditions": "Partly cloudy"}`

- `POST /weather/ask`: Ask the weather agent a question
  - Request body: `{"query": "What's the weather in Tokyo?", "city": "TOKYO"}`
  - Returns: `{"response": "The weather in Tokyo is currently sunny, with a temperature range of 20-30°C.", "weather_data": {...}, "trace_id": "..."}`

- `POST /weather/stream`: Stream a response from the weather agent
  - Request body: `{"query": "What's the weather in Tokyo?", "stream": true, "city": "TOKYO"}`

### Other Endpoints

- `GET /`: Root endpoint
- `GET /health`: Health check endpoint

## Example Usage

### Using curl

```bash
# Get a list of cities
curl http://localhost:8000/weather/cities

# Get weather for a specific city
curl http://localhost:8000/weather/NEW_YORK

# Ask the weather agent a question
curl -X POST -H "Content-Type: application/json" \
  -d '{"query": "What is the weather like in Tokyo?"}' \
  http://localhost:8000/weather/ask

# Stream a response from the weather agent
curl -X POST -H "Content-Type: application/json" \
  -d '{"query": "Tell me about London weather", "stream": true}' \
  http://localhost:8000/weather/stream
```

## Extending

### Adding More Agents

To add more agents, create new router files in the `app/routers` directory and include them in `app/__init__.py`.
