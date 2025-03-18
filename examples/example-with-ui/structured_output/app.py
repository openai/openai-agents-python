# app.py
import chainlit as cl
import nest_asyncio
from pydantic import BaseModel
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel, RunConfig
from dotenv import load_dotenv
import os
# Apply nest_asyncio to handle event loop in Chainlit
load_dotenv()
nest_asyncio.apply()

# Define the WeatherAnswer Pydantic model
class WeatherAnswer(BaseModel):
    location: str
    temperature_c: float
    summary: str

# Setup function that runs when the app starts
@cl.on_chat_start
async def setup_agent():
    try:
        # Get Gemini API key (adjust this based on your environment)
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set. Please ensure it is defined.")

        # Configure the external client for Gemini
        external_client = AsyncOpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        # Configure the model
        model = OpenAIChatCompletionsModel(
            model="gemini-2.0-flash",
            openai_client=external_client
        )

        # Configure run settings
        config = RunConfig(
            model=model,
            model_provider=external_client,
            tracing_disabled=True
        )

        # Create the agent
        agent = Agent(
            name="StructuredWeatherAgent",
            instructions="Use the final_output tool with WeatherAnswer schema.",
            output_type=WeatherAnswer
        )

        # Store the agent and config in the user session
        cl.user_session.set("agent", agent)
        cl.user_session.set("config", config)

        await cl.Message(
            content="Weather Agent is ready! Ask me about the weather in any location."
        ).send()

    except Exception as e:
        await cl.Message(content=f"Error during setup: {str(e)}").send()

# Main message handler
@cl.on_message
async def handle_message(message: cl.Message):
    try:
        # Get agent and config from session
        agent = cl.user_session.get("agent")
        config = cl.user_session.get("config")

        if not agent or not config:
            await cl.Message(content="Agent not initialized. Please restart the chat.").send()
            return

        # Run the agent with the user's query
        result = await Runner.run(agent, message.content, run_config=config)
        
        # Format the structured output
        if result.final_output:
            weather = result.final_output
            response = f"Weather in {weather.location}:\n" \
                      f"Temperature: {weather.temperature_c}°C\n" \
                      f"Summary: {weather.summary}"
        else:
            response = "Sorry, I couldn't get the weather information."

        # Send the response
        await cl.Message(content=response).send()

    except Exception as e:
        await cl.Message(content=f"Error: {str(e)}").send()

# Note: To run this, you'll need to:
# 1. Install required packages: pip install chainlit openai-agents pydantic
# 2. Set your GEMINI_API_KEY in your environment
# 3. Run with: chainlit run app.py