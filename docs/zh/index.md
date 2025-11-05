---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、抽象极少的方式构建智能体式 AI 应用。它是我们此前智能体试验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版。Agents SDK 仅包含一小组基本组件：

- **智能体**：配备指令与工具的 LLM
- **任务转移**：允许智能体将特定任务委派给其他智能体
- **安全防护措施**：用于验证智能体的输入与输出
- **会话**：在智能体多次运行之间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 内置 **追踪**，可用于可视化与调试你的智能体流程、对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的原因

该 SDK 的两条核心设计原则：

1. 功能足够有用，同时保持组件足够少，便于快速上手。
2. 开箱即用效果很好，同时支持精确自定义行为。

主要特性包括：

- 智能体循环：内置循环，处理调用工具、将结果发给 LLM，并循环直至 LLM 完成。
- Python 优先：使用语言内建特性编排与串联智能体，无需学习新的抽象。
- 任务转移：在多个智能体之间进行协调与委派的强大能力。
- 安全防护措施：与智能体并行运行输入校验与检查，检查失败时可提前中断。
- 会话：跨智能体运行自动管理对话历史，免去手动状态管理。
- 工具调用：将任意 Python 函数变为工具，自动生成模式，并由 Pydantic 驱动校验。
- 追踪：内置追踪，便于可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```