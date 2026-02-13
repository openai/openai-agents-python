---
search:
  exclude: true
---
# 智能体

智能体是你应用中的核心构建块。智能体是一个大语言模型（LLM），并配置了 instructions 和 tools。

## 基本配置

你将配置的智能体最常见属性包括：

-   `name`: 必需的字符串，用于标识你的智能体。
-   `instructions`: 也称为开发者消息或系统提示词。
-   `model`: 使用哪个 LLM，以及可选的 `model_settings`，用于配置 temperature、top_p 等模型调优参数。
-   `prompt`: 使用 OpenAI 的 Responses API 时，通过 id（以及变量）引用一个提示词模板。
-   `tools`: 智能体可用于完成任务的工具。
-   `mcp_servers`: 为智能体提供工具的 MCP 服务。参见 [MCP 指南](mcp.md)。
-   `reset_tool_choice`: 是否在一次工具调用后重置 `tool_choice`（默认：`True`），以避免工具使用循环。参见[强制使用工具](#forcing-tool-use)。

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

## 提示词模板

你可以通过设置 `prompt` 来引用在 OpenAI 平台中创建的提示词模板。这适用于使用 Responses API 的 OpenAI 模型。

请按以下步骤使用：

1. 前往 https://platform.openai.com/playground/prompts
2. 创建一个新的提示词变量 `poem_style`。
3. 创建一个系统提示词，内容为：

    ```
    Write a poem in {{poem_style}}
    ```

4. 使用 `--prompt-id` 标志运行该示例。

```python
from agents import Agent

agent = Agent(
    name="Prompted assistant",
    prompt={
        "id": "pmpt_123",
        "version": "1",
        "variables": {"poem_style": "haiku"},
    },
)
```

你也可以在运行时动态生成提示词：

```python
from dataclasses import dataclass

from agents import Agent, GenerateDynamicPromptData, Runner

@dataclass
class PromptContext:
    prompt_id: str
    poem_style: str


async def build_prompt(data: GenerateDynamicPromptData):
    ctx: PromptContext = data.context.context
    return {
        "id": ctx.prompt_id,
        "version": "1",
        "variables": {"poem_style": ctx.poem_style},
    }


agent = Agent(name="Prompted assistant", prompt=build_prompt)
result = await Runner.run(
    agent,
    "Say hello",
    context=PromptContext(prompt_id="pmpt_123", poem_style="limerick"),
)
```

## 上下文

智能体对其 `context` 类型是泛型的。上下文是一种依赖注入工具：它是你创建并传入 `Runner.run()` 的对象，会被传递给每个智能体、工具、任务转移等，并作为本次智能体运行的依赖与状态集合。你可以提供任何 Python 对象作为上下文。

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

默认情况下，智能体产生纯文本（即 `str`）输出。如果你希望智能体产生特定类型的输出，可以使用 `output_type` 参数。一个常见选择是使用 [Pydantic](https://docs.pydantic.dev/) 对象，但我们支持任何可被 Pydantic 的 [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) 包装的类型——dataclasses、lists、TypedDict 等。

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

    当你传入 `output_type` 时，这会告诉模型使用 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)，而不是常规的纯文本响应。

## 多智能体系统设计模式

构建多智能体系统的方法有很多，但我们通常看到两种广泛适用的模式：

1. 管理者（Agents as tools）：一个中央管理者/编排器将专门的子智能体作为工具来调用，并保留对对话的控制权。
2. 任务转移：对等智能体将控制权转移给一个专门的智能体，由其接管对话。这是去中心化的。

更多细节请参见[我们关于构建智能体的实用指南](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)。

### 管理者（Agents as tools）

`customer_facing_agent` 处理所有用户交互，并调用作为工具暴露的专门子智能体。更多内容请阅读 [tools](tools.md#agents-as-tools) 文档。

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

任务转移是智能体可以委派给的子智能体。当发生一次任务转移时，被委派的智能体会接收对话历史并接管对话。该模式支持模块化、专门化的智能体，使其在单一任务上表现出色。更多内容请阅读 [handoffs](handoffs.md) 文档。

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

## 动态 instructions

在大多数情况下，你可以在创建智能体时提供 instructions。不过，你也可以通过一个函数提供动态 instructions。该函数会接收智能体和上下文，并且必须返回提示词。支持普通函数与 `async` 函数。

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

有时，你希望观察智能体的生命周期。例如，你可能希望记录事件，或在某些事件发生时预取数据。你可以通过 `hooks` 属性接入智能体生命周期。对 [`AgentHooks`][agents.lifecycle.AgentHooks] 类进行子类化，并重写你感兴趣的方法。

## 安全防护措施

安全防护措施允许你在智能体运行的同时，并行对用户输入执行检查/校验，并在智能体输出生成后对其进行检查。例如，你可以筛查用户输入和智能体输出的相关性。更多内容请阅读 [guardrails](guardrails.md) 文档。

## 克隆/复制智能体

通过调用智能体的 `clone()` 方法，你可以复制一个 Agent，并可选地更改任意属性。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.2",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 强制使用工具

提供一组工具并不总是意味着 LLM 会使用工具。你可以通过设置 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] 来强制使用工具。有效取值为：

1. `auto`：允许 LLM 决定是否使用工具。
2. `required`：要求 LLM 使用工具（但它可以智能地决定使用哪个工具）。
3. `none`：要求 LLM _不_ 使用工具。
4. 设置为特定字符串，例如 `my_tool`：要求 LLM 使用该特定工具。

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

`Agent` 配置中的 `tool_use_behavior` 参数控制如何处理工具输出：

- `"run_llm_again"`：默认值。运行工具后，由 LLM 处理结果并生成最终响应。
- `"stop_on_first_tool"`：将第一次工具调用的输出直接作为最终响应，不再进行后续的 LLM 处理。

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

- `StopAtTools(stop_at_tool_names=[...])`：如果调用了任意指定工具则停止，并将其输出作为最终响应。

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

- `ToolsToFinalOutputFunction`：一个自定义函数，用于处理工具结果并决定停止还是继续让 LLM 运行。

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

    为防止无限循环，框架会在一次工具调用后自动将 `tool_choice` 重置为 "auto"。该行为可通过 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] 配置。无限循环的原因是：工具结果会被发送给 LLM，而 LLM 又会因为 `tool_choice` 生成另一次工具调用，如此往复，无穷无尽。