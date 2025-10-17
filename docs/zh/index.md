---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 使你能够以轻量、易用、抽象极少的方式构建智能体 AI 应用。它是我们此前智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版。Agents SDK 仅包含一小组基本组件：

- **智能体**：配备指令和工具的 LLM
- **任务转移**：允许智能体将特定任务委派给其他智能体
- **安全防护措施**：对智能体的输入与输出进行验证
- **会话**：在多次运行中自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭的学习曲线即可构建真实世界的应用。此外，SDK 内置了 **追踪**，可视化并调试你的智能体流程，对其进行评估，甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 的两条核心设计原则：

1. 功能足够丰富以值得使用，但基本组件足够精简以便快速上手。
2. 开箱即用效果出色，同时允许你精确自定义行为。

SDK 的主要特性包括：

- 智能体循环：内置循环来调用工具、将结果发送给 LLM，并循环直至 LLM 完成。
- Python 优先：使用语言内置特性编排与串联智能体，无需学习新的抽象。
- 任务转移：在多个智能体之间进行协调与委派的强大能力。
- 安全防护措施：与智能体并行运行输入验证与检查，一旦失败即可提前终止。
- 会话：跨多次运行自动管理对话历史，免去手动状态处理。
- 工具调用：将任意 Python 函数转换为工具，自动生成模式并使用 Pydantic 进行验证。
- 追踪：内置追踪以可视化、调试并监控工作流，同时可使用 OpenAI 的评估、微调与蒸馏工具套件。

## 安装

```bash
pip install openai-agents
```

## Hello World 示例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（运行前请确保已设置 `OPENAI_API_KEY` 环境变量）

```bash
export OPENAI_API_KEY=sk-...
```