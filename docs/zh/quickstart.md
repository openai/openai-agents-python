---
search:
  exclude: true
---
# 快速入门

## 创建项目和虚拟环境

你只需要做一次。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 激活虚拟环境

每次开始新的终端会话时都要执行此操作。

```bash
source .venv/bin/activate
```

### 安装 Agents SDK

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### 设置 OpenAI API 密钥

如果你还没有，请按照[这些说明](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)创建 OpenAI API 密钥。

```bash
export OPENAI_API_KEY=sk-...
```

## 创建你的第一个智能体

智能体通过 instructions、名称以及可选配置（例如特定模型）来定义。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 运行你的第一个智能体

使用 [`Runner`][agents.run.Runner] 执行智能体，并获取返回的 [`RunResult`][agents.result.RunResult]。

```python
import asyncio
from agents import Agent, Runner

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)

async def main():
    result = await Runner.run(agent, "When did the Roman Empire fall?")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

在第二轮中，你可以将 `result.to_input_list()` 传回 `Runner.run(...)`，附加一个 [session](sessions/index.md)，或使用 `conversation_id` / `previous_response_id` 复用由 OpenAI 服务端管理的状态。[运行智能体](running_agents.md)指南对这些方法进行了比较。

可参考以下经验法则：

| 如果你想要... | 建议从...开始 |
| --- | --- |
| 完全手动控制且与提供商无关的历史记录 | `result.to_input_list()` |
| 由 SDK 为你加载和保存历史记录 | [`session=...`](sessions/index.md) |
| 由 OpenAI 管理的服务端续接 | `previous_response_id` 或 `conversation_id` |

有关权衡和精确行为，请参见[运行智能体](running_agents.md#choose-a-memory-strategy)。

## 为你的智能体提供工具

你可以为智能体提供工具来查找信息或执行操作。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool
def history_fun_fact() -> str:
    """Return a short history fact."""
    return "Sharks are older than trees."


agent = Agent(
    name="History Tutor",
    instructions="Answer history questions clearly. Use history_fun_fact when it helps.",
    tools=[history_fun_fact],
)


async def main():
    result = await Runner.run(
        agent,
        "Tell me something surprising about ancient life on Earth.",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

## 再添加几个智能体

在选择多智能体模式之前，先决定由谁来负责最终答案：

-   **任务转移**：某位专家接管该轮对话中的这部分内容。
-   **Agents as tools**：编排器保持控制，并将专家作为工具调用。

本快速入门继续使用**任务转移**，因为这是最简短的首个示例。关于管理者风格模式，请参阅[智能体编排](multi_agent.md)和[工具：Agents as tools](tools.md#agents-as-tools)。

其他智能体也可以用同样方式定义。`handoff_description` 会为路由智能体提供额外上下文，以判断何时委派。

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You answer history questions clearly and concisely.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You explain math step by step and include worked examples.",
)
```

## 定义你的任务转移

在一个智能体上，你可以定义一个可对外发起的任务转移选项清单，以便它在解决任务时进行选择。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 运行智能体编排

Runner 会处理执行各个智能体、任何任务转移以及任何工具调用。

```python
import asyncio
from agents import Runner


async def main():
    result = await Runner.run(
        triage_agent,
        "Who was the first president of the United States?",
    )
    print(result.final_output)
    print(f"Answered by: {result.last_agent.name}")


if __name__ == "__main__":
    asyncio.run(main())
```

## 参考代码示例

该仓库包含了相同核心模式的完整脚本：

-   [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py) 用于首次运行。
-   [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py) 用于工具调用。
-   [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py) 用于多智能体路由。

## 查看追踪

要查看智能体运行期间发生了什么，请前往 [OpenAI 控制台中的 Trace viewer](https://platform.openai.com/traces) 查看智能体运行的追踪。

## 后续步骤

了解如何构建更复杂的智能体流程：

-   了解如何配置[智能体](agents.md)。
-   了解[运行智能体](running_agents.md)和[sessions](sessions/index.md)。
-   了解[tools](tools.md)、[安全防护措施](guardrails.md)和[模型](models/index.md)。