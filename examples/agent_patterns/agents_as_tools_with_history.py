import asyncio
import tempfile
from typing import cast

from agents import Agent, Runner, SQLiteSession, trace
from agents.tool import function_tool

"""
This example shows how an agent-as-tool can access the entire conversation history
using a session. The session information is declared globally or stored in the
user context and tools can then read that session and have the conversation history
for all turns up to that point.
"""

spanish_agent = Agent(
    name="spanish_agent",
    instructions="You translate the user's message to Spanish",
    handoff_description="An english to spanish translator",
)

french_agent = Agent(
    name="french_agent",
    instructions="You translate the user's message to French",
    handoff_description="An english to french translator",
)

italian_agent = Agent(
    name="italian_agent",
    instructions="You translate the user's message to Italian",
    handoff_description="An english to italian translator",
)


async def main(session_id, file_path):
    @function_tool
    async def check_tool() -> str:
        """A tool that looks at the entire conversation, checks
        your work, and asks for corrections if needed.

        Returns
        -------
        str
            A string starting with either REJECTED or ACCEPTED,
            followed by instructions to correct the translations, if needed.
        """

        # Open the session and get all the messages as input items
        _session = SQLiteSession(session_id=session_id, db_path=file_path)
        input_messages = await _session.get_items()

        check_agent = Agent(
            name="check_agent",
            instructions=(
                "Check the translations for correctness by reviewing the entire conversation and "
                "asking for corrections if needed.  Respond with either REJECTED or ACCEPTED, "
                "followed by instructions to correct the translations, if needed."
            ),
        )

        output = await Runner.run(
            check_agent,
            input=input_messages,
        )
        return cast(str, output.final_output)

    orchestrator_agent = Agent(
        name="orchestrator_agent",
        instructions=(
            "You are a translation agent. Translate the user's text "
            "to Spanish, Italian and French.  Always use the provided tools. "
            "After translating the text, you mush call the check_tool to "
            "ensure correctness."
        ),
        tools=[
            spanish_agent.as_tool(
                tool_name="translate_to_spanish",
                tool_description="Translate the user's message to Spanish",
            ),
            french_agent.as_tool(
                tool_name="translate_to_french",
                tool_description="Translate the user's message to French",
            ),
            italian_agent.as_tool(
                tool_name="translate_to_italian",
                tool_description="Translate the user's message to Italian",
            ),
            check_tool,
        ],
    )

    msg = input("Hi! What would you like translated? ")

    # Create a new SQLite session with the provided session ID and file path
    session = SQLiteSession(
        session_id=session_id,
        db_path=file_path,
    )

    # Run the entire orchestration in a single trace
    with trace("Orchestrator evaluator"):
        orchestrator_result = await Runner.run(orchestrator_agent, msg, session=session)

    print(f"\n\nFinal response:\n{orchestrator_result.final_output}")


if __name__ == "__main__":
    with tempfile.NamedTemporaryFile("+r", suffix=".db") as db_file:
        # Create a new SQLite session with the temporary database file
        session_id = "ABCDEF"
        file_path = db_file

        # Run the main function
        asyncio.run(main(session_id, file_path))

        # This input triggers a "REJECTED" response from the check_tool most of the time
        # "hello, guten tag, hola, bonjure"
