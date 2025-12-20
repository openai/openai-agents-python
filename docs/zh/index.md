---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 以轻量、易用、极少抽象的形式，帮助你构建具备智能体能力的 AI 应用。它是我们此前针对智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产环境的升级版。Agents SDK 仅包含一小组基本组件（primitives）：

- **Agents**，配备指令和工具的 LLM
- **Handoffs**，使智能体能够将特定任务委派给其他智能体的机制
- **Guardrails**，用于对智能体的输入与输出进行验证
- **Sessions**，自动在多次智能体运行间维护会话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭的学习曲线即可构建真实世界的应用。此外，SDK 内置**追踪**，可用于可视化与调试智能体流程，并支持评估，甚至为你的应用对模型进行微调。

## 使用 Agents SDK 的理由

该 SDK 的设计遵循两条原则：

1. 功能足够丰富值得使用，同时保持足够少的基本组件，便于快速上手。
2. 开箱即用体验优秀，同时允许你精确自定义执行细节。

SDK 的主要特性包括：

- 智能体循环：内置循环，负责调用工具、将结果发回 LLM，并循环直至 LLM 完成。
- Python 优先：使用语言内建特性编排与串联智能体，无需学习全新抽象。
- 任务转移：强大的能力，用于在多个智能体之间协调与委派。
- 安全防护措施：与智能体并行执行输入验证与检查，若检查失败则提前中断。
- 会话：在多次智能体运行间自动管理会话历史，免去手动状态处理。
- 工具调用：将任意 Python 函数变为工具，自动生成模式，并使用 Pydantic 进行验证。
- 追踪：内置追踪，用于可视化、调试与监控工作流，并可配合 OpenAI 的评估、微调与蒸馏工具。

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

(_如果要运行，请确保设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```