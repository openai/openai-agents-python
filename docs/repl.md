# REPL utility

The SDK provides `run_demo_loop` for quick, interactive testing of an agent's behavior directly in your terminal.


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` prompts for user input in a loop, keeping the conversation history between turns. By default, it streams model output as it is produced. When you run the example above, run_demo_loop starts an interactive chat session. It continuously asks for your input, remembers the entire conversation history between turns (so your agent knows what's been discussed) and automatically streams the agent's responses to you in real-time as they are generated.

To end this chat session, simply type `quit` or `exit` (and press Enter) or use the `Ctrl-D` keyboard shortcut.


## Few-shot initialization with `preload_history`

The `run_demo_loop` function accepts an optional `preload_history` parameter, which lets you seed the session with predefined user/assistant exchanges.  
This enables few-shot prompting by giving the agent example interactions that establish context and behavior before the interactive session begins.

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    preload_history = [
        {"role": "user", "content": "What's 2+2?"},
        {"role": "assistant", "content": "2+2 equals 4."}
    ]
    await run_demo_loop(agent, preload_history=preload_history)

if __name__ == "__main__":
    asyncio.run(main())

```

When you run this example, the session begins with the provided history.
The agent continues the conversation naturally, as if those exchanges had already taken place.