import asyncio
import uuid

from openai.types.responses import ResponseContentPartDoneEvent, ResponseTextDeltaEvent
from agents import Agent, RawResponsesStreamEvent, Runner, trace, SQLiteSession

french_agent = Agent(name="french_agent", instructions="You only speak French")
spanish_agent = Agent(name="spanish_agent", instructions="You only speak Spanish")
english_agent = Agent(name="english_agent", instructions="You only speak English")

triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
)

async def main():
    conversation_id = str(uuid.uuid4().hex[:16])
    # Create a persistent session for this conversation
    session = SQLiteSession(conversation_id, "conversation_history.db")

    msg = input("Hi! We speak French, Spanish and English. How can I help? ")

    # We use a trace to track the full conversation
    with trace("Routing example", group_id=conversation_id):
        result = Runner.run_streamed(
            triage_agent,
            input=[{"content": msg, "role": "user"}],
            session=session,  # Pass the session object here
        )
        async for event in result.stream_events():
            if not isinstance(event, RawResponsesStreamEvent):
                continue
            data = event.data
            if isinstance(data, ResponseTextDeltaEvent):
                print(data.delta, end="", flush=True)
            elif isinstance(data, ResponseContentPartDoneEvent):
                print("\n")

        while True:
            print("\n")
            user_msg = input("Enter a message: ")
            
            # The session automatically remembers the previous conversation
            result = Runner.run_streamed(
                result.current_agent,
                input=[{"content": user_msg, "role": "user"}],
                session=session,  # The session is now the source of history
            )
            async for event in result.stream_events():
                if not isinstance(event, RawResponsesStreamEvent):
                    continue
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent):
                    print(data.delta, end="", flush=True)
                elif isinstance(data, ResponseContentPartDoneEvent):
                    print("\n")

if __name__ == "__main__":
    asyncio.run(main())