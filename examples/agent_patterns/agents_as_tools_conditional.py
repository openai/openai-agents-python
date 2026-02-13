import asyncio

from pydantic import BaseModel

from agents import Agent, AgentBase, ModelSettings, RunContextWrapper, Runner, trace
from agents.tool import function_tool
from examples.auto_mode import confirm_with_fallback, input_with_fallback

"""
This example demonstrates the agents-as-tools pattern with conditional tool enabling.
Agent tools are dynamically enabled/disabled based on user access levels using the
is_enabled parameter.
"""


class AppContext(BaseModel):
    language_preference: str = "spanish_only"  # "spanish_only", "french_spanish", "european"


def french_spanish_enabled(ctx: RunContextWrapper[AppContext], agent: AgentBase) -> bool:
    """Enable for French+Spanish and European preferences."""
    return ctx.context.language_preference in ["french_spanish", "european"]


def european_enabled(ctx: RunContextWrapper[AppContext], agent: AgentBase) -> bool:
    """Only enable for European preference."""
    return ctx.context.language_preference == "european"


@function_tool(needs_approval=True)
async def get_user_name() -> str:
    print("Getting the user's name...")
    return "Kaz"


# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish. You must call all the tools to best answer the user's question.",
    model_settings=ModelSettings(tool_choice="required"),
    tools=[get_user_name],
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

italian_agent = Agent(
    name="italian_agent",
    instructions="You respond in Italian. Always reply to the user's question in Italian.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
            needs_approval=True,  # HITL
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_spanish_enabled,
        ),
        italian_agent.as_tool(
            tool_name="respond_italian",
            tool_description="Respond to the user's question in Italian",
            is_enabled=european_enabled,
        ),
    ],
)


async def main():
    """Interactive demo with LLM interaction."""
    print("Agents-as-Tools with Conditional Enabling\n")
    print(
        "This demonstrates how language response tools are dynamically enabled based on user preferences.\n"
    )

    print("Choose language preference:")
    print("1. Spanish only (1 tool)")
    print("2. French and Spanish (2 tools)")
    print("3. European languages (3 tools)")

    choice = input_with_fallback("\nSelect option (1-3): ", "2").strip()
    preference_map = {"1": "spanish_only", "2": "french_spanish", "3": "european"}
    language_preference = preference_map.get(choice, "spanish_only")

    # Create context and show available tools
    context = RunContextWrapper(AppContext(language_preference=language_preference))
    available_tools = await orchestrator.get_all_tools(context)
    tool_names = [tool.name for tool in available_tools]

    print(f"\nLanguage preference: {language_preference}")
    print(f"Available tools: {', '.join(tool_names)}")
    print(f"The LLM will only see and can use these {len(available_tools)} tools\n")

    # Get user request
    user_request = input_with_fallback(
        "Ask a question and see responses in available languages:\n",
        "How do you say good morning?",
    )

    # Run with LLM interaction
    print("\nProcessing request...")
    with trace("Conditional tool access"):
        result = await Runner.run(
            starting_agent=orchestrator,
            input=user_request,
            context=context.context,
        )
        while result.interruptions:

            async def confirm(question: str) -> bool:
                return confirm_with_fallback(f"{question} (y/n): ", default=True)

            state = result.to_state()
            for interruption in result.interruptions:
                prompt = f"\nDo you approve this tool call: {interruption.name} with arguments {interruption.arguments}?"
                confirmed = await confirm(prompt)
                if confirmed:
                    state.approve(interruption)
                    print(f"✓ Approved: {interruption.name}")
                else:
                    state.reject(interruption)
                    print(f"✗ Rejected: {interruption.name}")
            result = await Runner.run(orchestrator, state)

    print(f"\nResponse:\n{result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
