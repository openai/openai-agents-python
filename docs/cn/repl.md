---
search:
  exclude: true
---
# REPL 实用工具

SDK 提供了 `run_demo_loop`，用于直接在终端中快速、交互式地测试智能体的行为。

  
```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示用户输入，并在轮次之间保持对话历史。默认情况下，它会随着生成而流式传输模型输出。当你运行上面的示例时，run_demo_loop 会启动一个交互式聊天会话。它会持续要求你的输入，在轮次之间记住整个对话历史（这样你的智能体就知道讨论了什么），并自动实时地将智能体的响应流式传输给你，就像它们生成时一样。

要结束此聊天会话，只需输入 `quit` 或 `exit`（然后按 Enter）或使用 `Ctrl-D` 键盘快捷键。