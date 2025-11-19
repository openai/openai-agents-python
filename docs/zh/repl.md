---
search:
  exclude: true
---
# REPL 实用工具

该 SDK 提供 `run_demo_loop`，可在你的终端中直接对智能体的行为进行快速、交互式测试。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示用户输入，并在每轮之间保留对话历史。默认情况下，它会在生成时流式传输模型输出。运行上面的示例时，run_demo_loop 会启动一个交互式聊天会话。它会持续请求你的输入，在轮次之间记住完整的对话历史（使你的智能体了解已讨论的内容），并在生成过程中实时将智能体的回复以流式传输方式输出给你。

若要结束此聊天会话，只需输入 `quit` 或 `exit`（并按 Enter），或使用 `Ctrl-D` 键盘快捷键。