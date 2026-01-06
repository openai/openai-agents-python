---
search:
  exclude: true
---
# REPL 实用工具

该 SDK 提供 `run_demo_loop`，可在终端中直接对智能体的行为进行快速、交互式测试。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 在循环中提示用户输入，并在各轮之间保留会话历史。默认情况下，它会在模型生成输出时进行流式传输。运行上面的示例后，run_demo_loop 会启动一个交互式聊天会话。它会持续请求你的输入，记住各轮之间的完整会话历史（因此你的智能体知道之前讨论了什么），并在生成时将智能体的回复实时流式传输给你。

要结束此聊天会话，只需输入 `quit` 或 `exit`（然后按 Enter），或使用 `Ctrl-D` 键盘快捷键。