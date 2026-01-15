---
search:
  exclude: true
---
# REPL 实用程序

该 SDK 提供 `run_demo_loop`，用于在终端中直接对智能体的行为进行快速、交互式测试。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示输入用户消息，并在回合之间保留对话历史。默认情况下，它会在模型生成输出时进行流式传输。运行上面的示例后，run_demo_loop 会启动一个交互式聊天会话。它会持续请求你的输入、在回合之间记住完整的对话历史（使你的智能体知道已讨论的内容），并在生成时将智能体的响应自动实时流式传输给你。

要结束该聊天会话，只需输入 `quit` 或 `exit`（然后按回车），或使用 `Ctrl-D` 键盘快捷键。