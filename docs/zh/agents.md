---
search:
  exclude: true
---
# 智能体

智能体是你应用中的核心构建模块。一个智能体是经过配置了 instructions 和工具的大型语言模型（LLM）。

## 基本配置

你通常会为智能体配置以下常见属性：

- `name`: 用于标识智能体的必填字符串。
- `instructions`: 也称为开发者消息或 system prompt。
- `model`: 指定要使用的 LLM，并可选提供 `model_settings` 来配置如 temperature、top_p 等模型调优参数。
- `tools`: 智能体可用于完成任务的工具。

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="gpt-5-nano",
    tools=[get_weather],
)
```

## 上下文

智能体对其 `context` 类型是泛型的。Context 是一种依赖注入工具：它是你创建并传递给 `Runner.run()` 的对象，会传递给每个智能体、工具、任务转移等，并作为此次智能体运行的依赖与状态集合。你可以提供任何 Python 对象作为 context。

```python
@dataclass
class UserContext:
    name: str
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 输出类型

默认情况下，智能体生成纯文本（即 `str`）输出。如果你希望智能体生成特定类型的输出，可以使用 `output_type` 参数。常见做法是使用 [Pydantic](https://docs.pydantic.dev/) 对象，但我们支持任何可以被 Pydantic 的[类型适配器（TypeAdapter）](https://docs.pydantic.dev/latest/api/type_adapter/)封装的类型——如 dataclasses、lists、TypedDict 等。

```python
from pydantic import BaseModel
from agents import Agent


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(
    name="Calendar extractor",
    instructions="Extract calendar events from text",
    output_type=CalendarEvent,
)
```

!!! note

    当你传入 `output_type` 时，这会让模型使用 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) 而不是常规纯文本响应。

## 多智能体系统设计模式

设计多智能体系统的方法有很多，但我们通常看到两种普适模式：

1. 管理器（智能体作为工具）：一个中心管理器/编排器将专业化的子智能体作为工具调用，并保持对话控制权。
2. 任务转移：对等智能体将控制权移交给专业化智能体，由其接管对话。这是去中心化的。

更多详情参见[我们的构建智能体实用指南](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)。

### 管理器（智能体作为工具）

`customer_facing_agent` 处理所有用户交互，并调用作为工具暴露的专业化子智能体。更多内容见[工具](tools.md#agents-as-tools)文档。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

customer_facing_agent = Agent(
    name="Customer-facing agent",
    instructions=(
        "Handle all direct user communication. "
        "Call the relevant tools when specialized expertise is needed."
    ),
    tools=[
        booking_agent.as_tool(
            tool_name="booking_expert",
            tool_description="Handles booking questions and requests.",
        ),
        refund_agent.as_tool(
            tool_name="refund_expert",
            tool_description="Handles refund questions and requests.",
        )
    ],
)
```

### 任务转移

任务转移是智能体可委派的子智能体。当发生任务转移时，被委派的智能体会接收对话历史并接管对话。该模式支持模块化、专精于单一任务的智能体。更多内容见[任务转移](handoffs.md)文档。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions. "
        "If they ask about booking, hand off to the booking agent. "
        "If they ask about refunds, hand off to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 动态指令

在大多数情况下，你可以在创建智能体时提供指令。不过，你也可以通过函数提供动态指令。该函数将接收智能体和 context，并且必须返回提示词。支持普通函数和 `async` 函数。

```python
def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## 生命周期事件（hooks）

有时你可能希望观察智能体的生命周期。例如，你可能希望记录事件，或在某些事件发生时预取数据。你可以通过 `hooks` 属性接入智能体生命周期。继承 [`AgentHooks`][agents.lifecycle.AgentHooks] 类，并重写你感兴趣的方法。

## 安全防护措施

安全防护措施允许你在智能体运行的同时并行地对用户输入进行检查/校验，并在智能体产出输出后对其进行检查。例如，你可以筛查用户输入和智能体输出的相关性。更多内容见[安全防护措施](guardrails.md)文档。

## 克隆/复制智能体

通过在智能体上使用 `clone()` 方法，你可以复制一个智能体，并可选地修改任意你想更改的属性。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-4.1",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 强制使用工具

提供工具列表并不总是意味着 LLM 会使用工具。你可以通过设置 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] 来强制使用工具。有效值包括：

1. `auto`：允许 LLM 自行决定是否使用工具。
2. `required`：要求 LLM 使用某个工具（但可智能选择使用哪个工具）。
3. `none`：要求 LLM 不使用工具。
4. 设置特定字符串，例如 `my_tool`，要求 LLM 使用该特定工具。

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    model_settings=ModelSettings(tool_choice="get_weather")
)
```

## 工具使用行为

在 `Agent` 配置中的 `tool_use_behavior` 参数控制如何处理工具输出：

- `"run_llm_again"`：默认值。运行工具后，由 LLM 处理结果以生成最终响应。
- `"stop_on_first_tool"`：首次工具调用的输出将作为最终响应，不再进行 LLM 后续处理。

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior="stop_on_first_tool"
)
```

- `StopAtTools(stop_at_tool_names=[...])`：当调用任一指定工具时停止，并使用其输出作为最终响应。

```python
from agents import Agent, Runner, function_tool
from agents.agent import StopAtTools

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

@function_tool
def sum_numbers(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b

agent = Agent(
    name="Stop At Stock Agent",
    instructions="Get weather or sum numbers.",
    tools=[get_weather, sum_numbers],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["get_weather"])
)
```

- `ToolsToFinalOutputFunction`：用于处理工具结果并决定是停止还是继续由 LLM 处理的自定义函数。

```python
from agents import Agent, Runner, function_tool, FunctionToolResult, RunContextWrapper
from agents.agent import ToolsToFinalOutputResult
from typing import List, Any

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

def custom_tool_handler(
    context: RunContextWrapper[Any],
    tool_results: List[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    """Processes tool results to decide final output."""
    for result in tool_results:
        if result.output and "sunny" in result.output:
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=f"Final weather: {result.output}"
            )
    return ToolsToFinalOutputResult(
        is_final_output=False,
        final_output=None
    )

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior=custom_tool_handler
)
```

!!! note

    为防止无限循环，框架会在一次工具调用后自动将 `tool_choice` 重置为 "auto"。你可以通过 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] 配置此行为。出现无限循环的原因是工具结果会被发送给 LLM，而由于 `tool_choice` 设置，LLM 会再次生成工具调用，如此往复。