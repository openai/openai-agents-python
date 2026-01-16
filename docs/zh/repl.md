---
search:
  exclude: true
---
# REPL 实用程序

该 SDK 提供 `run_demo_loop`，可在终端中快速、交互式地测试智能体的行为。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示用户输入，并在轮次之间保留对话历史。默认情况下，它会在模型生成输出时进行流式传输。当你运行上面的示例时，run_demo_loop 会启动一个交互式聊天会话。它会持续询问你的输入，在轮次之间记住完整的对话历史（因此你的智能体知道已讨论的内容），并在生成过程中实时自动将智能体的响应流式传输给你。

要结束此聊天会话，只需输入 `quit` 或 `exit`（然后按回车），或使用 `Ctrl-D` 键盘快捷键。