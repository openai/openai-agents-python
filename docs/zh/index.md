---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、几乎无抽象的方式构建基于智能体的 AI 应用。它是我们此前针对智能体的探索 [Swarm](https://github.com/openai/swarm/tree/main) 的生产可用升级版。Agents SDK 只包含一小组基本组件：

- **智能体**，即配备了 instructions 和工具的 LLM
- **任务转移**，使智能体能将特定任务委托给其他智能体
- **安全防护措施**，用于校验智能体的输入与输出
- **会话**，在智能体多次运行间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你在没有陡峭学习曲线的情况下构建真实应用。此外，SDK 内置了**追踪**，可帮助你可视化并调试智能体流程、进行评估，甚至为你的应用微调模型。

## 为何使用 Agents SDK

该 SDK 的两项核心设计原则：

1. 功能足够多到值得使用，但基本组件足够少以便快速上手。
2. 开箱即用效果佳，同时你可以精确定制实际行为。

SDK 的主要特性包括：

- 智能体循环：内置循环，负责调用工具、将结果发送给 LLM，并循环直至 LLM 完成。
- Python 优先：使用内置语言特性来编排与串联智能体，而无需学习新的抽象。
- 任务转移：强大的能力，用于在多个智能体之间协调与委派。
- 安全防护措施：并行运行输入校验与检查，若检查失败可提前中断。
- 会话：跨智能体运行自动管理对话历史，免去手动状态处理。
- 工具调用：将任意 Python 函数变为工具，自动生成模式并提供基于 Pydantic 的校验。
- 追踪：内置追踪，支持可视化、调试与监控工作流，并可使用 OpenAI 的评测、微调与蒸馏工具。

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

(_如果运行此示例，请确保设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```