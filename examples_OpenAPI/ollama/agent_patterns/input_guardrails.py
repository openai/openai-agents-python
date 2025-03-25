import asyncio
import sys
import os

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
)
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates how to use input guardrails.

Guardrails are checks that run in parallel with agent execution. They can be used to:
- Check if input messages are off-topic
- Check if output messages violate any policies
- Take over control of agent execution if unexpected input is detected

In this example, we set up an input guardrail that triggers when the user asks for help 
with math homework. If the guardrail is triggered, we respond with a rejection message.
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

### 1. Agent-based guardrail that triggers when user asks for math homework help
class MathHomeworkOutput(BaseModel):
    reasoning: str
    is_math_homework: bool


guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking you to do their math homework.",
    output_type=MathHomeworkOutput,
    model_settings=create_ollama_settings()
)


@input_guardrail
async def math_guardrail(
    context: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    """This is an input guardrail function that calls an agent to check if the input is a math homework question."""
    result = await Runner.run(guardrail_agent, input, context=context.context, run_config=run_config)
    final_output = result.final_output_as(MathHomeworkOutput)

    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=final_output.is_math_homework,
    )


### 2. The run loop


async def main():
    agent = Agent(
        name="Customer Support Agent",
        instructions="You are a customer support agent. You help customers answer their questions.",
        input_guardrails=[math_guardrail],
        model_settings=create_ollama_settings()
    )

    input_data: list[TResponseInputItem] = []

    print("Running Input Guardrails Example with Ollama")
    print("Try asking normal questions, then try asking math homework questions (like 'help me solve the equation: 2x + 5 = 11')")
    print("Enter 'exit' to quit")
    
    while True:
        user_input = input("\nEnter a message: ")
        if user_input.lower() == 'exit':
            break
            
        input_data.append(
            {
                "role": "user",
                "content": user_input,
            }
        )

        print("Processing...")
        try:
            result = await Runner.run(agent, input_data, run_config=run_config)
            print(result.final_output)
            # If guardrail wasn't triggered, use the result for the next run
            input_data = result.to_input_list()
        except InputGuardrailTripwireTriggered:
            # If guardrail triggers, add a rejection message to the input
            message = "Sorry, I cannot help with math homework."
            print(message)
            input_data.append(
                {
                    "role": "assistant",
                    "content": message,
                }
            )


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
