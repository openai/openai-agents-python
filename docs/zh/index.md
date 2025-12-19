---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、少抽象的方式构建智能体式 AI 应用。它是我们此前针对智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产级升级版。Agents SDK 仅包含一小组基本组件：

-   **智能体**：配备 instructions 和 tools 的 LLM
-   **任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：用于验证智能体的输入与输出
-   **会话**：在多次运行中自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 内置 **追踪**，可视化并调试你的智能体流程，支持评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

该 SDK 的两项核心设计原则：

1. 功能足够有用，同时保持组件尽量精简，便于快速上手。
2. 开箱即用，并允许你精细定制具体行为。

SDK 的主要特性如下：

-   智能体循环：内置循环，负责调用工具、将结果发送给 LLM，并循环直到 LLM 完成。
-   Python 优先：用语言内置特性编排与串联智能体，无需学习新的抽象。
-   任务转移：在多个智能体间进行协调与委派的强大能力。
-   安全防护措施：与智能体并行运行输入校验与检查，若检查失败可提前中止。
-   会话：在多次运行中自动管理对话历史，免去手动状态处理。
-   工具调用：将任意 Python 函数转换为工具，自动生成模式并通过 Pydantic 进行校验。
-   追踪：内置追踪，可视化、调试与监控你的工作流，并使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果要运行，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```