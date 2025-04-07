import os
from openai import OpenAI

openai_client = OpenAI(base_url="http://localhost:8080/v1", api_key=os.getenv("OPENAI_API_KEY"), default_headers={'x-model-provider': 'openai'})

response = openai_client.responses.create(
    model="gpt-4o-mini",
    input="Tell me a joke"
)
print("Generated response:", response.output[0].content[0].text)