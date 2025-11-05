---
search:
  exclude: true
---
# REPL 工具

SDK 提供了 `run_demo_loop`，可在终端中快速、交互式地测试智能体的行为。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示输入用户输入，并在多轮对话间保留会话历史。默认情况下，它会以流式传输的方式输出模型生成的内容。运行上面的示例时，run_demo_loop 会启动一个交互式聊天会话。它会持续请求你的输入、在多轮对话间记住整个会话历史（因此你的智能体知道之前讨论了什么），并在生成时自动将智能体的响应实时流式传输给你。

要结束此聊天会话，只需输入 `quit` 或 `exit`（并按下回车），或使用 `Ctrl-D` 键盘快捷键。