"""
Example demonstrating MnemoPay integration with the OpenAI Agents SDK.

MnemoPay gives agents persistent memory and micropayment capabilities via an MCP
server.  This example shows a research assistant that:

1. Recalls prior user preferences from memory.
2. Stores new preferences for future sessions.
3. Charges for research work and settles payment via escrow.
4. Checks its own wallet balance and reputation.

Prerequisites:
    pip install mnemopay-openai-agents

Run:
    python -m examples.mnemopay.agent_memory_wallet
"""

import asyncio

from agents import Agent, Runner

# The mnemopay_openai_agents package wraps 13 MCP tools as OpenAI Agents
# function_tools. Install via: pip install mnemopay-openai-agents
try:
    from mnemopay_openai_agents import mnemopay_tools
except ImportError as err:
    raise SystemExit(
        "mnemopay-openai-agents is not installed. Run: pip install mnemopay-openai-agents"
    ) from err

INSTRUCTIONS = """\
You are a research assistant with persistent memory and a wallet.

**Memory rules**
- At the start of every conversation, use `recall` to check for prior context.
- When the user shares a preference or important fact, use `remember` to store it.
- After a memory proves useful, use `reinforce` to boost its importance.

**Payment rules**
- Only use `charge` AFTER delivering real value (a summary, analysis, or answer).
- Always explain the charge reason and amount before calling `charge`.
- Use `settle` to finalize escrow once the user confirms satisfaction.

**Observability**
- If the user asks about your status, use `profile` or `balance`.
"""


async def main() -> None:
    agent = Agent(
        name="Research Assistant",
        instructions=INSTRUCTIONS,
        tools=mnemopay_tools(),
    )

    print("=== MnemoPay Research Assistant ===")
    print("The agent has persistent memory and payment tools.\n")

    # Turn 1 -- agent recalls prior context and stores a new preference.
    print("Turn 1: Establishing preferences")
    print("User: I prefer concise bullet-point summaries. Remember that.\n")
    result = await Runner.run(
        agent,
        "I prefer concise bullet-point summaries. Remember that.",
    )
    print(f"Assistant: {result.final_output}\n")

    # Turn 2 -- agent uses memory to tailor output and charges for work.
    print("Turn 2: Research request")
    print("User: Summarize the key benefits of MCP for AI agents.\n")
    result = await Runner.run(
        agent,
        "Summarize the key benefits of MCP (Model Context Protocol) for AI agents.",
    )
    print(f"Assistant: {result.final_output}\n")

    # Turn 3 -- check agent profile.
    print("Turn 3: Status check")
    print("User: What is your current balance and reputation?\n")
    result = await Runner.run(
        agent,
        "What is your current balance and reputation?",
    )
    print(f"Assistant: {result.final_output}\n")

    print("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
