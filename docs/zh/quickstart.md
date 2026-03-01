---
search:
  exclude: true
---
# 快速入门

## 创建项目和虚拟环境

你只需要执行一次。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 激活虚拟环境

每次开始新的终端会话时都要执行。

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

智能体由 instructions、名称以及可选配置（例如特定模型）定义。

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

对于第二轮，你可以将 `result.to_input_list()` 传回 `Runner.run(...)`，附加一个 [session](sessions/index.md)，或通过 `conversation_id` / `previous_response_id` 复用 OpenAI 服务端托管状态。[运行智能体](running_agents.md)指南对这些方法进行了比较。

## 为智能体提供工具

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

## 添加更多智能体

可以用同样的方式定义其他智能体。`handoff_description` 会为路由智能体提供关于何时委派的额外上下文。

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

在智能体上，你可以定义一个可选的外发任务转移选项清单，以便其在解决任务时进行选择。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 运行智能体编排

运行器会处理单个智能体的执行、任何任务转移以及任何工具调用。

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

仓库包含了相同核心模式的完整脚本：

-   首次运行请参考 [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)。
-   工具调用请参考 [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)。
-   多智能体路由请参考 [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)。

## 查看追踪

要查看智能体运行期间发生了什么，请前往 [OpenAI Dashboard 中的追踪查看器](https://platform.openai.com/traces) 查看智能体运行的追踪。

## 后续步骤

了解如何构建更复杂的智能体流程：

-   了解如何配置[智能体](agents.md)。
-   了解[运行智能体](running_agents.md)和[sessions](sessions/index.md)。
-   了解[工具](tools.md)、[安全防护措施](guardrails.md)和[模型](models/index.md)。