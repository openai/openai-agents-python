# REPL utility

The SDK provides `run_demo_loop` for quick interactive testing.

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` prompts for user input in a loop, keeping the conversation
history between turns. By default it streams model output as it is produced.
Type `quit` or `exit` (or press `Ctrl-D`) to leave the loop.
