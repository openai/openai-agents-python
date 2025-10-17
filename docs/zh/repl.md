---
search:
  exclude: true
---
# REPL 工具

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

`run_demo_loop` 在循环中提示输入用户内容，并在各轮次之间保留对话历史。默认情况下，它会在生成时以流式传输的方式输出模型结果。运行上述示例后，run_demo_loop 会启动一个交互式聊天会话。它会持续询问你的输入、在轮次之间记住整个对话历史（因此你的智能体知道已经讨论过什么），并在生成过程中实时自动将智能体的回复流式传输给你。

要结束此聊天会话，只需输入 `quit` 或 `exit`（然后按回车），或使用 `Ctrl-D` 键盘快捷键。