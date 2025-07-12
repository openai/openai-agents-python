import asyncio
import uuid

from openai.types.responses import ResponseContentPartDoneEvent, ResponseTextDeltaEvent
from agents import (
    Agent,
    RawResponsesStreamEvent,
    Runner,
    SQLiteSession,
    trace,
)

# Define your agents
french_agent = Agent(name="french_agent", instructions="You only speak French")
spanish_agent = Agent(name="spanish_agent", instructions="You only speak Spanish")
english_agent = Agent(name="english_agent", instructions="You only speak English")

triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
)

async def main():
    # 1) Create a unique session ID and backing store
    conversation_id = str(uuid.uuid4().hex[:16])
    session = SQLiteSession(conversation_id)

    # 2) First user prompt
    msg = input("Hi! We speak French, Spanish and English. How can I help? ")
    agent = triage_agent

    # 3) Streamed loop; session memory handles the history
    while True:
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

if __name__ == "__main__":
    asyncio.run(main())
