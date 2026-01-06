---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、几乎无抽象的方式构建面向智能体的 AI 应用。它是我们此前针对智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产级升级版。Agents SDK 仅包含一小组基本组件：

- **智能体（Agents）**：配备 instructions 和 tools 的 LLM
- **任务转移（Handoffs）**：允许智能体将特定任务委派给其他智能体
- **安全防护措施（Guardrails）**：对智能体的输入与输出进行验证
- **会话（Sessions）**：在多次智能体运行间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，使你无需陡峭的学习曲线即可构建真实世界应用。此外，SDK 内置 **追踪（tracing）**，便于可视化与调试你的智能体流程，并支持评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

该 SDK 的设计原则有二：

1. 功能足够实用，同时保持足够少的基本组件，便于快速上手。
2. 开箱即用表现优秀，同时你可以精确自定义每一步行为。

SDK 的主要特性包括：

- 智能体循环：内置循环负责调用工具、将结果反馈给 LLM，并循环直至 LLM 完成。
- Python 优先：使用内置语言特性来编排与串联智能体，无需学习新的抽象。
- 任务转移：在多个智能体之间进行协调与委派的强大能力。
- 安全防护措施：与智能体并行运行输入校验与检查，检查失败则可提前中止。
- 会话：跨智能体运行自动管理对话历史，免去手动状态管理。
- 工具调用：将任意 Python 函数变为工具，支持自动生成模式定义并通过 Pydantic 驱动校验。
- 追踪：内置追踪用于可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果要运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```