---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、极少抽象的方式构建智能体 AI 应用。它是我们此前针对智能体的试验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版。Agents SDK 仅包含一小组基本组件：

- **智能体（Agents）**，即配备了 instructions 和 tools 的 LLMs
- **任务转移（Handoffs）**，允许智能体将特定任务委派给其他智能体
- **安全防护措施（Guardrails）**，用于对智能体的输入和输出进行校验
- **会话（Sessions）**，在多次运行智能体时自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体间的复杂关系，让你无需陡峭学习曲线即可构建真实应用。此外，SDK 内置了 **追踪（tracing）**，便于可视化与调试智能体流程，并进一步对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

该 SDK 遵循两条核心设计原则：

1. 功能足够丰富以值得使用，但基本组件足够少以便于快速上手。
2. 开箱即用表现优秀，同时允许你精细定制实际行为。

SDK 的主要特性包括：

- Agent 循环：内置 agent 循环，负责调用工具、将结果返回给 LLM，并循环直至 LLM 完成。
- Python 优先：使用内置语言特性来编排与串联智能体，而无需学习新的抽象。
- 任务转移：在多个智能体之间进行协调与委派的强大能力。
- 安全防护措施：与智能体并行运行输入校验与检查，若检查失败可提前中止。
- 会话：在多次运行智能体时自动管理对话历史，免去手动维护状态。
- 工具调用：将任意 Python 函数转换为工具，自动生成模式并通过 Pydantic 进行校验。
- 追踪：内置追踪，便于可视化、调试与监控工作流，并使用 OpenAI 的评估、微调与蒸馏工具套件。

## 安装

```bash
pip install openai-agents
```

## Hello world 示例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（如果运行该示例，请确保已设置 `OPENAI_API_KEY` 环境变量）

```bash
export OPENAI_API_KEY=sk-...
```