---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、极少抽象的方式构建基于智能体的 AI 应用。这是我们此前针对智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版。Agents SDK 仅包含一小组基本组件：

- **智能体（Agents）**：配备 instructions 和 tools 的 LLM
- **任务转移（Handoffs）**：允许智能体将特定任务委派给其他智能体
- **安全防护措施（Guardrails）**：用于验证智能体的输入与输出
- **会话（Sessions）**：在多次运行间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 内置 **追踪（tracing）**，可用于可视化与调试智能体流程，并支持评估、以及为你的应用对模型进行微调。

## 使用 Agents SDK 的理由

该 SDK 的两条核心设计原则：

1. 功能足够有用，同时基本组件足够少，便于快速上手。
2. 开箱即用效果出色，同时支持精确自定义行为。

SDK 的主要特性包括：

- 智能体循环：内置循环处理工具调用、将结果发送给 LLM，并持续循环直至 LLM 完成。
- Python 优先：使用语言自身特性来编排并串联智能体，而无需学习新的抽象。
- 任务转移：在多个智能体之间进行协调与委派的强大功能。
- 安全防护措施：与智能体并行运行输入校验与检查，若检查失败则提前中断。
- 会话：在多次运行间自动管理对话历史，免去手动状态处理。
- 工具调用（function tools）：将任意 Python 函数转为工具，自动生成模式并借助 Pydantic 进行校验。
- 追踪（tracing）：内置追踪，可视化、调试与监控工作流，并使用 OpenAI 的评估、微调与蒸馏工具套件。

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