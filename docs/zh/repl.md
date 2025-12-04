---
search:
  exclude: true
---
# REPL 实用工具

该 SDK 提供 `run_demo_loop`，可在终端中对智能体行为进行快速、交互式测试。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` 会在循环中提示用户输入，并在回合间保留对话历史。默认情况下，它会以流式传输的方式输出模型生成结果。运行上述示例时，run_demo_loop 会启动一个交互式聊天会话。它会持续询问你的输入，在回合之间记住整个对话历史（因此你的智能体知道已讨论的内容），并在生成时自动实时将智能体的响应以流式传输方式发送给你。

要结束该聊天会话，只需输入 `quit` 或 `exit`（并按回车），或使用 `Ctrl-D` 键盘快捷键。