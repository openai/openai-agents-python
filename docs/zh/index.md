---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用且极少抽象的方式构建智能体 AI 应用。它是我们之前用于智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版本。Agents SDK 仅包含一小组基本组件：

- **智能体**：配备指令和工具的 LLM
- **任务转移**：使智能体能够将特定任务委派给其他智能体
- **安全防护措施**：支持对智能体的输入与输出进行校验
- **会话**：在多次运行中自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线就能构建真实世界应用。此外，SDK 内置 **追踪**，用于可视化与调试智能体流程，并能对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

该 SDK 的两个核心设计原则：

1. 功能足够有用，同时保持足够少的基本组件以便快速上手。
2. 开箱即用表现优秀，同时允许你精细定制执行过程。

SDK 的主要特性如下：

- 智能体循环：内置循环，负责调用工具、将结果返回给 LLM，并在 LLM 完成前自动循环。
- Python 优先：使用内置语言特性编排与串联智能体，而无需学习新的抽象。
- 任务转移：用于在多个智能体之间协调与委派的强大能力。
- 安全防护措施：与智能体并行运行输入校验与检查，如检查失败则提前中断。
- 会话：跨多次运行自动管理对话历史，免去手动状态处理。
- 工具调用：将任意 Python 函数变为工具，自动生成模式，并由 Pydantic 提供校验。
- 追踪：内置追踪，便于可视化、调试与监控工作流；还可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_If running this, ensure you set the `OPENAI_API_KEY` environment variable_)

```bash
export OPENAI_API_KEY=sk-...
```