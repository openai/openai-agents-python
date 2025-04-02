
from openai import OpenAI
import os

# Set custom parameters directly
BASE_URL = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1" #Either set OPEN_RESPONSES_URL in environment variable or put it directly here.
API_KEY = os.getenv("GROK_API_KEY") or "" #Either set GROK_API_KEY in environment variable or put it directly here.
MODEL_NAME = "qwen-2.5-32b"

# Define custom headers explicitly
custom_headers = {
    "Authorization": f"Bearer {API_KEY}"
}

# Create a custom OpenAI client with the custom URL, API key, and explicit headers via default_headers.
client = OpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    default_headers=custom_headers
)

history = [
    {
        "role": "user",
        "content": "tell me a joke"
    }
]

response = client.responses.create(
    model=MODEL_NAME,
    input=history,
    store=True
)

print(response.output_text)

# Add the response to the conversation

second_response = client.responses.create(
    model=MODEL_NAME,
    previous_response_id=response.id,
    input=[{"role": "user", "content": "explain why this is funny."}],
)
print(second_response.output_text)