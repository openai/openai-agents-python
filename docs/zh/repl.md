---
search:
  exclude: true
---
# REPL 工具

SDK 提供了 `run_demo_loop`，可在你的终端中快速、交互式地测试智能体的行为。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示用户输入，并在轮次之间保留对话历史。默认情况下，它会在模型生成内容时进行流式传输。运行上述示例后，run_demo_loop 会启动一个交互式聊天会话。它会持续等待你的输入，在各轮之间记住完整的对话历史（让你的智能体知道已经讨论了什么），并在生成的同时实时以流式方式将智能体的回复发送给你。

要结束此聊天会话，只需输入 `quit` 或 `exit`（并按 Enter），或使用 `Ctrl-D` 键盘快捷键。