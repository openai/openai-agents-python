# OpenAI Agents API

This project provides a FastAPI application that exposes OpenAI agents through a RESTful API.

## Features

- FastAPI-based RESTful API for OpenAI agents
- Weather agent with mock data (extensible to real weather APIs)
- Development server with hot-reloading
- Example client for API consumption

## Requirements

- Python 3.8+
- Required packages are listed in `requirements.txt`

## Setup

1. Install the required packages:

```bash
pip install -r requirements.txt
```

2. Run the development server:

```bash
./dev_appserver.py --reload
```

This will start the server at http://127.0.0.1:8000

## API Endpoints

### Weather Agent

- `POST /api/v1/weather/ask`: Ask the weather agent a question
  - Request body: `{"query": "What's the weather in Tokyo?", "stream": false, "city": "Tokyo"}`
  - Returns: `{"response": "...", "weather_data": {...}, "trace_id": "..."}`

- `POST /api/v1/weather/stream`: Stream a response from the weather agent (placeholder)
  - Request body: `{"query": "What's the weather in Tokyo?", "stream": true, "city": "Tokyo"}`

### Other Endpoints

- `GET /`: Root endpoint
- `GET /health`: Health check endpoint

## Example Usage

Run the example client:

```bash
./examples/api_client_example.py --query "What's the weather in London?"
```

## Development Options

The development server supports the following options:

- `--host`: Host to bind the server to (default: 127.0.0.1)
- `--port`: Port to bind the server to (default: 8000)
- `--reload`: Enable auto-reload on code changes
- `--debug`: Enable debug mode
- `--workers`: Number of worker processes (default: 1)

## Extending

To add more agents, create new router files in the `app/routers` directory and include them in `app/main.py`.
