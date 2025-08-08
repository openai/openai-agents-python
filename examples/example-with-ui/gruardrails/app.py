import os
from dotenv import load_dotenv
import chainlit as cl
import nest_asyncio
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
    output_guardrail,
    AsyncOpenAI,
    OpenAIChatCompletionsModel,
    RunConfig
)

load_dotenv()

# Apply nest_asyncio for Chainlit's event loop
nest_asyncio.apply()

# Pydantic models for structured outputs
class MathHomeworkOutput(BaseModel):
    is_math_homework: bool
    reasoning: str

class MessageOutput(BaseModel):
    response: str

class MathOutput(BaseModel):
    is_math: bool
    reasoning: str

# Input guardrail function
@input_guardrail
async def math_guardrail(
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    guardrail_agent = cl.user_session.get("guardrail_agent")
    config = cl.user_session.get("config")
    result = await Runner.run(guardrail_agent, input, context=ctx.context, run_config=config)
    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=result.final_output.is_math_homework,
    )

# Output guardrail function
@output_guardrail
async def math_guardrail2(
    ctx: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    guardrail_agent2 = cl.user_session.get("guardrail_agent2")
    config = cl.user_session.get("config")
    result = await Runner.run(guardrail_agent2, output.response, context=ctx.context, run_config=config)
    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=result.final_output.is_math,
    )

# Setup function to initialize agents and config
@cl.on_chat_start
async def setup_agents():
    try:
        # Get Gemini API key (adjust based on your environment)
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set.")

        # Configure external client
        external_client = AsyncOpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        # Configure model
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

        # Input guardrail agent
        guardrail_agent = Agent(
            name="Guardrail check",
            instructions="Check if the user is asking you to do their math homework.",
            output_type=MathHomeworkOutput,
        )

        # Output guardrail agent
        guardrail_agent2 = Agent(
            name="Guardrail check",
            instructions="Check if the output includes any math.",
            output_type=MathOutput,
        )

        # Main customer support agent with both guardrails
        agent = Agent(
            name="Customer support agent",
            instructions="You are a customer support agent. You help customers with their questions.",
            input_guardrails=[math_guardrail],
            output_guardrails=[math_guardrail2],
            output_type=MessageOutput,
        )

        # Store everything in session
        cl.user_session.set("agent", agent)
        cl.user_session.set("guardrail_agent", guardrail_agent)
        cl.user_session.set("guardrail_agent2", guardrail_agent2)
        cl.user_session.set("config", config)

        await cl.Message(
            content="Customer Support Agent is ready! Ask me anything (but no math homework!)."
        ).send()

    except Exception as e:
        await cl.Message(content=f"Setup error: {str(e)}").send()

# Message handler
@cl.on_message
async def handle_message(message: cl.Message):
    try:
        agent = cl.user_session.get("agent")
        config = cl.user_session.get("config")

        if not agent or not config:
            await cl.Message(content="Agent not initialized. Please restart the chat.").send()
            return

        # Run the agent with guardrails
        try:
            result = await Runner.run(agent, message.content, run_config=config)
            response = result.final_output.response if result.final_output else "I couldn't process that request."
            await cl.Message(content=response).send()

        except InputGuardrailTripwireTriggered:
            await cl.Message(content="Sorry, I can't help with math homework!").send()
        except OutputGuardrailTripwireTriggered:
            await cl.Message(content="My response contained math, which I'm not allowed to provide.").send()

    except Exception as e:
        await cl.Message(content=f"Error: {str(e)}").send()

