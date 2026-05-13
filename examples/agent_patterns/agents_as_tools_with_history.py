"""
Agent as tool with conversation history.

Demonstrates ``include_conversation_history=True`` on ``Agent.as_tool()``.
The orchestrator delegates to two sub-agents:

- **analyst** (with history): sees the full conversation via a <CONVERSATION HISTORY>
  summary, so it can reference earlier facts and tool results.
- **blind** (without history): sees only the tool input string, proving the default
  behavior is unchanged.

Try telling the orchestrator some facts, then asking it to delegate to the analyst
or blind agent to see the difference.
"""

import asyncio

from agents import Agent, Runner, TResponseInputItem

analyst = Agent(
    name="analyst",
    instructions=(
        "You analyze conversations. Reference specific facts, names, and numbers "
        "from the conversation history to show you have full context."
    ),
)

blind = Agent(
    name="blind",
    instructions="Answer questions based on whatever conversation you can see.",
)

orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a helpful assistant. For normal questions, answer directly.\n"
        "You have two tools:\n"
        "- ask_analyst: delegate to an analyst who can see the FULL conversation history\n"
        "- ask_blind: delegate to an agent WITHOUT conversation history\n"
        "Use the appropriate tool when asked."
    ),
    tools=[
        analyst.as_tool(
            tool_name="ask_analyst",
            tool_description="Delegate to the analyst (has full conversation history).",
            include_conversation_history=True,
        ),
        blind.as_tool(
            tool_name="ask_blind",
            tool_description="Delegate to the blind agent (has NO conversation history).",
            include_conversation_history=False,
        ),
    ],
)


async def main():
    print("=== Agent as Tool with Conversation History ===")
    print("Chat with the orchestrator. It can delegate to the analyst (with history)")
    print("or blind agent (without history).")
    print("Type 'quit' to exit.\n")

    items: list[TResponseInputItem] = []

    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.strip().lower() in ("quit", "exit", "q"):
            break

        items.append({"role": "user", "content": user_input})
        result = await Runner.run(orchestrator, items)
        print(f"\nOrchestrator: {result.final_output}\n")
        items = result.to_input_list()


if __name__ == "__main__":
    asyncio.run(main())
