import asyncio
import uuid

from openai.types.responses import ResponseContentPartDoneEvent, ResponseTextDeltaEvent 
from agents import (
    Agent,
    RawResponsesStreamEvent,
    Runner,
    SQLiteSession,
    trace,
    AsyncOpenAI,
    OpenAIChatCompletionsModel,
)
from dotenv import load_dotenv , find_dotenv
import os

# ─────────────────────────────────────────────────────────────
# 1) credentials & configure tracing
# ─────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())

api_key = os.getenv("GEMINI_API_KEY")

os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")


client = AsyncOpenAI(
    api_key=api_key,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

model = OpenAIChatCompletionsModel(
    model="gemini-2.0-flash",
    openai_client=client,
)

# Define your agents
french_agent = Agent(name="french_agent", instructions="You only speak French" , model=model)
spanish_agent = Agent(name="spanish_agent", instructions="You only speak Spanish" , model=model)
english_agent = Agent(name="english_agent", instructions="You only speak English" ,  model=model)

triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
    model=model
)

french_agent.handoffs.append(english_agent)


EXIT_CAMMANDS = ["quit", "exit" , "bye" , "stop"]


async def main():
    # 1) Create a unique session ID and backing store
    conversation_id = str(uuid.uuid4().hex[:16])
    session = SQLiteSession(conversation_id)

    # 2) First user prompt
    msg = input("Hi! We speak French, Spanish and English. How can I help? ")
    agent = triage_agent

    # 3) Streamed loop; session memory handles the history
    while True:
        if msg in EXIT_CAMMANDS:
            break
        with trace("Routing example", group_id=conversation_id):
            result = Runner.run_streamed(
                agent,
                input=msg,
                session=session,
            )

            async for event in result.stream_events():
                if not isinstance(event, RawResponsesStreamEvent):
                    continue

                data = event.data
                if isinstance(data, ResponseTextDeltaEvent):
                    print(data.delta, end="", flush=True)
                elif isinstance(data, ResponseContentPartDoneEvent):
                    print()  # end‑of‑response newline

        # 4) Get next user message and routed agent
        msg = input("Enter a message: ")
        agent = result.current_agent
        print(agent.name)

if __name__ == "__main__":
    asyncio.run(main())
