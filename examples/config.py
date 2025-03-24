import os
from agents import set_default_openai_client, AsyncOpenAI

api_key = os.getenv("OPENAI_API_KEY") or "" #Either set API_KEY in environment variable or put it directly here.
base_url = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1" #Either set OPEN_RESPONSES_URL in environment variable or put it directly here.

set_default_openai_client(
    AsyncOpenAI(api_key=api_key, base_url=base_url)
)