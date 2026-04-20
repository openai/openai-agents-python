"""Example of using MLflow AI Gateway as the LLM backend for OpenAI Agents SDK.

MLflow AI Gateway (MLflow >= 3.0) is a database-backed LLM proxy that routes
requests to multiple providers through a single OpenAI-compatible endpoint.
Provider API keys are stored encrypted on the server.

Setup:
    1. pip install mlflow[genai]
    2. mlflow server --host 127.0.0.1 --port 5000
    3. Create a gateway endpoint in the MLflow UI at http://localhost:5000
       (AI Gateway → Create Endpoint)
    4. Set environment variables:
       export MLFLOW_GATEWAY_URL="http://localhost:5000/gateway/openai/v1"
       export MLFLOW_GATEWAY_ENDPOINT="my-chat-endpoint"  # your endpoint name

Usage:
    python examples/model_providers/mlflow_gateway_example.py
"""

import asyncio
import os

from openai import AsyncOpenAI

from agents import (
    Agent,
    Runner,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)

GATEWAY_URL = os.getenv("MLFLOW_GATEWAY_URL", "http://localhost:5000/gateway/openai/v1")
ENDPOINT_NAME = os.getenv("MLFLOW_GATEWAY_ENDPOINT", "my-chat-endpoint")

# Configure the SDK to use MLflow AI Gateway.
# 1. Create a client pointing to the gateway's OpenAI-compatible endpoint.
# 2. Set it as the default client (don't use it for OpenAI tracing).
# 3. Use Chat Completions API (gateway doesn't support Responses API).
client = AsyncOpenAI(
    base_url=GATEWAY_URL,
    api_key="unused",  # provider keys are managed by the MLflow server
)
set_default_openai_client(client=client, use_for_tracing=False)
set_default_openai_api("chat_completions")

# Disable OpenAI platform tracing since we're using a custom endpoint.
# For MLflow-native tracing, use mlflow.openai.autolog() instead.
set_tracing_disabled(disabled=True)


@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Keep answers concise.",
        model=ENDPOINT_NAME,
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
