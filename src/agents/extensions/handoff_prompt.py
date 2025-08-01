# A recommended prompt prefix for agents that use handoffs. We recommend including this or
# similar instructions in any agents that use handoffs.

RECOMMENDED_PROMPT_PREFIX = (
    "# System context\n"
    "You are part of a multi-agent system called the Agents SDK, designed to make agent "
    "coordination and execution easy. Agents uses two primary abstraction: **Agents** and "
    "**Handoffs**. An agent encompasses instructions and tools and can hand off a "
    "conversation to another agent when appropriate. "
    "Handoffs are achieved by calling a handoff function, generally named "
    "`transfer_to_<agent_name>`. Transfers between agents are handled seamlessly in the background;"
    " do not mention or draw attention to these transfers in your conversation with the user.\n"
)


def prompt_with_handoff_instructions(prompt: str) -> str:
    """
    Add recommended instructions to the prompt for agents that use handoffs.

    This function takes a user prompt and prepends it with standard instructions defined in
    RECOMMENDED_PROMPT_PREFIX. These instructions guide agents on how to handle handoffs,
    ensuring seamless transitions between agents in the Agents SDK.

    Args:
        prompt (str): The original user or system prompt (e.g., "What's the weather?").

    Returns:
        str: The enhanced prompt with handoff instructions added.

    Example:
        Input: "What's the weather?"
        Output:
        # System context
        You are part of a multi-agent system called the Agents SDK, designed to make agent coordination and execution easy. Agents uses two primary abstraction: **Agents** and **Handoffs**. An agent encompasses instructions and tools and can hand off a conversation to another agent when appropriate. Handoffs are achieved by calling a handoff function, generally named `transfer_to_<agent_name>`. Transfers between agents are handled seamlessly in the background; do not mention or draw attention to these transfers in your conversation with the user.

        What's the weather?
    """
    return f"{RECOMMENDED_PROMPT_PREFIX}\n\n{prompt}"