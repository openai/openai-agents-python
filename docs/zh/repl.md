---
search:
  exclude: true
---
# REPL 实用程序

SDK 提供了 `run_demo_loop`，可在终端中直接对智能体行为进行快速、交互式测试。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示用户输入，并在回合之间保留对话历史。默认情况下，它会在模型生成输出时进行流式传输。运行上述示例后，run_demo_loop 会启动一个交互式聊天会话。它会持续请求你的输入，在回合之间记住整个对话历史（因此你的智能体知道已经讨论了什么），并在生成过程中自动实时流式传输智能体的响应给你。

要结束此聊天会话，只需输入 `quit` 或 `exit`（并按回车），或使用 `Ctrl-D` 键盘快捷键。