import asyncio
import sys
import os
import json

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel, Field

from src.agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    output_guardrail,
)
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates how to use output guardrails.

Output guardrails are checks run on the agent's final output. They can be used to:
- Check if the output contains sensitive data
- Check if the output is a valid response to the user's message

In this example, we use a (contrived) example to check if the agent's response contains a phone number.
"""

def create_ollama_settings(model="phi3:latest"):
    """Create Ollama model settings"""
    return ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model=model,
        temperature=0.7
    )

# Create run configuration
run_config = RunConfig(tracing_disabled=True)
# Set model provider
run_config.model_provider = ModelProviderFactory.create_provider(create_ollama_settings())

# Output type for the agent
class MessageOutput(BaseModel):
    reasoning: str = Field(description="Thoughts about how to respond to the user's message")
    response: str = Field(description="Response to the user's message")
    user_name: str | None = Field(description="Name of the user who sent the message, if known")


@output_guardrail
async def sensitive_data_check(
    context: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    phone_number_in_response = "650" in output.response
    phone_number_in_reasoning = "650" in output.reasoning

    return GuardrailFunctionOutput(
        output_info={
            "phone_number_in_response": phone_number_in_response,
            "phone_number_in_reasoning": phone_number_in_reasoning,
        },
        tripwire_triggered=phone_number_in_response or phone_number_in_reasoning,
    )


agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
    output_type=MessageOutput,
    output_guardrails=[sensitive_data_check],
    model_settings=create_ollama_settings()
)


async def main():
    print("Running Output Guardrails Example with Ollama")
    
    # This should be fine
    print("Testing normal question...")
    result1 = await Runner.run(agent, "What is the capital of California?", run_config=run_config)
    print("First message passed")
    print(f"Output: {json.dumps(result1.final_output.model_dump(), indent=2, ensure_ascii=False)}")

    print("\nTesting question with phone number...")
    # This should trigger the guardrail
    try:
        result2 = await Runner.run(
            agent, "My phone number is 650-123-4567. Where do you think I live?", run_config=run_config
        )
        print(
            f"Guardrail not triggered - this is unexpected. Output: {json.dumps(result2.final_output.model_dump(), indent=2, ensure_ascii=False)}"
        )

    except OutputGuardrailTripwireTriggered as e:
        print(f"Guardrail triggered. Info: {e.guardrail_result.output.output_info}")


if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned a non-200 status code. Make sure Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Could not connect to Ollama service. Make sure Ollama service is running.\n{str(e)}")
        print("\nIf you haven't installed Ollama yet, download and install it from https://ollama.ai and start the service with 'ollama serve'")
        sys.exit(1)
        
    # Run the main function
    asyncio.run(main())
