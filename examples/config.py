import os
from agents import set_default_openai_client, AsyncOpenAI

api_key = os.getenv("API_KEY")  # Fetch the key from an env variable named API_KEY
base_url = "http://localhost:8080/v1"

set_default_openai_client(
    AsyncOpenAI(api_key=api_key, base_url=base_url)
)