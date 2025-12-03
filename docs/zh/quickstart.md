---
search:
  exclude: true
---
# 快速开始

## 创建项目和虚拟环境

你只需执行一次。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 激活虚拟环境

每次开启新的终端会话时都需要执行。

```bash
source .venv/bin/activate
```

### 安装 Agents SDK

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### 设置 OpenAI API 密钥

如果你还没有，请按照[这些说明](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)创建一个 OpenAI API 密钥。

```bash
export OPENAI_API_KEY=sk-...
```

## 创建你的第一个智能体

智能体由 instructions、名称和可选配置（例如 `model_config`）定义。

```python
from agents import Agent

agent = Agent(
    name="Math Tutor",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)
```

## 添加更多智能体

可以用相同方式定义其他智能体。`handoff_descriptions` 为确定任务转移路由提供额外上下文。

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)
```

## 定义你的任务转移

在每个智能体上，你可以定义一个可用的外发任务转移选项清单，供智能体选择以决定如何推进其任务。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent]
)
```

## 运行智能体编排

让我们检查工作流是否运行，并且分诊智能体是否在两个专家型智能体之间正确路由。

```python
from agents import Runner

async def main():
    result = await Runner.run(triage_agent, "What is the capital of France?")
    print(result.final_output)
```

## 添加安全防护措施

你可以为输入或输出定义自定义安全防护措施。

```python
from agents import GuardrailFunctionOutput, Agent, Runner
from pydantic import BaseModel


class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )
```

## 集成运行

把以上全部组合起来，运行完整工作流，使用任务转移和输入安全防护措施。

```python
from agents import Agent, InputGuardrail, GuardrailFunctionOutput, Runner
from agents.exceptions import InputGuardrailTripwireTriggered
from pydantic import BaseModel
import asyncio

class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)


async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )

triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent],
    input_guardrails=[
        InputGuardrail(guardrail_function=homework_guardrail),
    ],
)

async def main():
    # Example 1: History question
    try:
        result = await Runner.run(triage_agent, "who was the first president of the united states?")
        print(result.final_output)
    except InputGuardrailTripwireTriggered as e:
        print("Guardrail blocked this input:", e)

    # Example 2: General/philosophical question
    try:
        result = await Runner.run(triage_agent, "What is the meaning of life?")
        print(result.final_output)
    except InputGuardrailTripwireTriggered as e:
        print("Guardrail blocked this input:", e)

if __name__ == "__main__":
    asyncio.run(main())
```

## 查看你的追踪

要回顾智能体运行期间发生的事情，请前往 [OpenAI 控制台的 Trace viewer](https://platform.openai.com/traces) 查看你的智能体运行追踪。

## 后续步骤

了解如何构建更复杂的智能体流程：

- 学习如何配置[智能体](agents.md)。
- 了解[运行智能体](running_agents.md)。
- 了解[tools](tools.md)、[安全防护措施](guardrails.md)和[模型](models/index.md)。