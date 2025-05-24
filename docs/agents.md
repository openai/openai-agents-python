# Agents

Agents are the core building block in your apps. An agent is a large language model (LLM), configured with instructions and tools.

## Basic configuration

The most common properties of an agent you'll configure are:

-   `instructions`: also known as a developer message or system prompt.
-   `model`: which LLM to use, and optional `model_settings` to configure model tuning parameters like temperature, top_p, etc.
-   `tools`: Tools that the agent can use to achieve its tasks.
-   `memory`: Enables conversation memory for the agent. Can be `bool | SessionMemory | None`.
    -   `True`: Uses the default `SQLiteSessionMemory` (in-memory by default, suitable for single-process applications).
    -   `SessionMemory instance`: Uses the provided custom memory implementation (e.g., for persistent storage or custom logic).
    -   `None` (default): No memory is used. The agent will not remember previous turns, and conversation history must be managed manually by passing all previous messages in the `input` to `Runner.run()`.

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## Context

Agents are generic on their `context` type. Context is a dependency-injection tool: it's an object you create and pass to `Runner.run()`, that is passed to every agent, tool, handoff etc, and it serves as a grab bag of dependencies and state for the agent run. You can provide any Python object as the context.

```python
@dataclass
class UserContext:
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## Output types

By default, agents produce plain text (i.e. `str`) outputs. If you want the agent to produce a particular type of output, you can use the `output_type` parameter. A common choice is to use [Pydantic](https://docs.pydantic.dev/) objects, but we support any type that can be wrapped in a Pydantic [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) - dataclasses, lists, TypedDict, etc.

```python
from pydantic import BaseModel
from agents import Agent


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(
    name="Calendar extractor",
    instructions="Extract calendar events from text",
    output_type=CalendarEvent,
)
```

!!! note

    When you pass an `output_type`, that tells the model to use [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) instead of regular plain text responses.

## Handoffs

Handoffs are sub-agents that the agent can delegate to. You provide a list of handoffs, and the agent can choose to delegate to them if relevant. This is a powerful pattern that allows orchestrating modular, specialized agents that excel at a single task. Read more in the [handoffs](handoffs.md) documentation.

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions."
        "If they ask about booking, handoff to the booking agent."
        "If they ask about refunds, handoff to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## Dynamic instructions

In most cases, you can provide instructions when you create the agent. However, you can also provide dynamic instructions via a function. The function will receive the agent and context, and must return the prompt. Both regular and `async` functions are accepted.

```python
def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## Lifecycle events (hooks)

Sometimes, you want to observe the lifecycle of an agent. For example, you may want to log events, or pre-fetch data when certain events occur. You can hook into the agent lifecycle with the `hooks` property. Subclass the [`AgentHooks`][agents.lifecycle.AgentHooks] class, and override the methods you're interested in.

## Guardrails

Guardrails allow you to run checks/validations on user input, in parallel to the agent running. For example, you could screen the user's input for relevance. Read more in the [guardrails](guardrails.md) documentation.

## Cloning/copying agents

By using the `clone()` method on an agent, you can duplicate an Agent, and optionally change any properties you like.

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="o3-mini",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## Agent Memory

The `memory` parameter on the `Agent` class allows you to easily enable conversation memory, so the agent can remember previous turns of a conversation.

When `memory` is enabled, the agent automatically loads history before calling the LLM and saves the new turn's interactions (input and output) after the LLM responds.

### Default Memory

Setting `memory=True` uses the default `SQLiteSessionMemory`, which stores the conversation in an in-memory SQLite database. This is convenient for quick setups and single-process applications.

```python
from agents import Agent, Runner # Ensure Runner is imported
import asyncio # For running async code

# Example for docs
async def run_conversation_with_default_memory():
    agent = Agent(
        name="ConversationalAgent",
        instructions="Remember our previous conversation. Be friendly!",
        model="o3-mini", # Assuming o3-mini is a valid model for your setup
        memory=True # Enable default SQLite memory
    )

    # Let's mock the LLM responses for predictable behavior in docs
    # In a real scenario, the LLM would generate these
    # For this example, we'll assume the LLM just acknowledges or uses memory.

    # First turn
    # Mocking LLM to just acknowledge.
    # In a real scenario, Runner.run would call the LLM.
    # For documentation, we often show illustrative interaction patterns.
    # Here, we'll simulate the interaction conceptually.
    
    print("Simulating conversation with default memory:")
    
    # Turn 1
    user_input_1 = "My favorite color is blue."
    print(f"User: {user_input_1}")
    # In a real run: result1 = await Runner.run(agent, user_input_1)
    # Simulated agent response:
    agent_response_1 = "Okay, I'll remember that your favorite color is blue."
    print(f"Agent: {agent_response_1}")
    # Manually add to memory for simulation continuity if not running real LLM
    if agent.memory: # Check if memory is enabled
        await agent.memory.add_items([
            {"role": "user", "content": user_input_1},
            {"role": "assistant", "content": agent_response_1} # Or the structured output
        ])


    # Turn 2
    user_input_2 = "What did I say my favorite color was?"
    print(f"User: {user_input_2}")
    # In a real run: result2 = await Runner.run(agent, user_input_2)
    # Simulated agent response (assuming LLM uses memory):
    # The LLM would have access to the history: [user: "My fav color is blue", assistant: "Okay..."]
    agent_response_2 = "You said your favorite color is blue."
    print(f"Agent: {agent_response_2}")

    # To actually run this example, you would need a configured model
    # and uncomment the Runner.run calls, e.g.:
    # agent_llm_mock = ... # setup a mock model for testing if needed
    # agent.model = agent_llm_mock 
    # result1 = await Runner.run(agent, user_input_1)
    # print(f"Agent: {result1.final_output}")
    # result2 = await Runner.run(agent, user_input_2)
    # print(f"Agent: {result2.final_output}")


# Example of how you might run it (if it were a fully runnable example):
# if __name__ == "__main__":
# asyncio.run(run_conversation_with_default_memory())
```

### Custom Memory

For more control, such as using persistent storage (e.g., a different database, file system) or implementing custom history management logic (e.g., summarization, windowing), you can provide your own session memory implementation. 

The [`SessionMemory`][agents.memory.SessionMemory] type is a `typing.Protocol` (specifically, a `@runtime_checkable` protocol). This means your custom memory class must define all the methods specified by the protocol (like `get_history`, `add_items`, `add_message`, and `clear`) with matching signatures. While explicit inheritance from `SessionMemory` is not strictly required by the protocol mechanism for runtime checks (thanks to `@runtime_checkable`), inheriting is still good practice for clarity and to help with static type checking.

The example below demonstrates creating a custom memory class by inheriting from `SessionMemory`:

```python
from agents.memory import SessionMemory, TResponseInputItem # Adjust imports as necessary

class MyCustomMemory(SessionMemory):
    def __init__(self):
        self.history: list[TResponseInputItem] = []

    async def get_history(self) -> list[TResponseInputItem]:
        # In a real implementation, this might fetch from a DB
        print(f"CustomMemory: Getting history (current length {len(self.history)})")
        return list(self.history) # Return a copy

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        # In a real implementation, this might save to a DB
        print(f"CustomMemory: Adding {len(items)} items.")
        self.history.extend(items)

    async def add_message(self, item: TResponseInputItem) -> None:
        # Helper, could be part of add_items
        print(f"CustomMemory: Adding 1 message.")
        self.history.append(item)

    async def clear(self) -> None:
        print("CustomMemory: Clearing history.")
        self.history.clear()

# How to use the custom memory:
custom_memory_instance = MyCustomMemory()
custom_agent = Agent(
    name="CustomMemoryAgent",
    instructions="I have a special memory.",
    model="o3-mini", # Example model
    memory=custom_memory_instance
)

# Example usage (conceptual)
async def run_with_custom_memory():
    print("\nSimulating conversation with custom memory:")
    user_q1 = "My name is Bob."
    print(f"User: {user_q1}")
    # await Runner.run(custom_agent, user_q1) # Actual run
    # Simulated interaction:
    await custom_agent.memory.add_items([{"role": "user", "content": user_q1}, {"role": "assistant", "content": "Nice to meet you, Bob!"}])
    print(f"Agent: Nice to meet you, Bob!")


    user_q2 = "What's my name?"
    print(f"User: {user_q2}")
    # history_for_llm = await custom_agent.memory.get_history()
    # print(f"History provided to LLM for 2nd turn: {history_for_llm}")
    # await Runner.run(custom_agent, user_q2) # Actual run
    # Simulated interaction:
    print(f"Agent: Your name is Bob.") # Assuming LLM uses memory

# if __name__ == "__main__":
# asyncio.run(run_conversation_with_default_memory())
# asyncio.run(run_with_custom_memory())

```
As mentioned, the `SessionMemory` protocol defines `get_history`, `add_items`, `add_message`, and `clear` methods that your custom class must implement.


## Forcing tool use

Supplying a list of tools doesn't always mean the LLM will use a tool. You can force tool use by setting [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice]. Valid values are:

1. `auto`, which allows the LLM to decide whether or not to use a tool.
2. `required`, which requires the LLM to use a tool (but it can intelligently decide which tool).
3. `none`, which requires the LLM to _not_ use a tool.
4. Setting a specific string e.g. `my_tool`, which requires the LLM to use that specific tool.

!!! note

    To prevent infinite loops, the framework automatically resets `tool_choice` to "auto" after a tool call. This behavior is configurable via [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice]. The infinite loop is because tool results are sent to the LLM, which then generates another tool call because of `tool_choice`, ad infinitum.

    If you want the Agent to completely stop after a tool call (rather than continuing with auto mode), you can set [`Agent.tool_use_behavior="stop_on_first_tool"`] which will directly use the tool output as the final response without further LLM processing.
