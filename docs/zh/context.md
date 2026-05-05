---
search:
  exclude: true
---
# 上下文管理

Context 是一个含义广泛的术语。你可能关心的上下文主要有两类：

1. 你的代码在本地可用的上下文：这是在工具函数运行时、在 `on_handoff` 等回调中、在生命周期钩子中等场景下可能需要的数据和依赖。
2. LLM 可用的上下文：这是 LLM 在生成回复时能看到的数据。

## 本地上下文

这通过 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类及其中的 [`context`][agents.run_context.RunContextWrapper.context] 属性来表示。其工作方式如下：

1. 你可以创建任何想要的 Python 对象。常见模式是使用 dataclass 或 Pydantic 对象。
2. 你将该对象传给各类 run 方法（例如 `Runner.run(..., context=whatever)`）。
3. 你的所有工具调用、生命周期钩子等都会收到一个包装器对象 `RunContextWrapper[T]`，其中 `T` 表示你的上下文对象类型，你可以通过 `wrapper.context` 访问它。

对于某些运行时特定回调，SDK 可能会传入 `RunContextWrapper[T]` 的更专用子类。例如，工具调用生命周期钩子通常会收到 `ToolContext`，它还会暴露工具调用元数据，如 `tool_call_id`、`tool_name` 和 `tool_arguments`。

**最重要**的一点是：在某次给定的智能体运行中，每个智能体、工具函数、生命周期等都必须使用相同的上下文_类型_。

你可以将上下文用于如下场景：

- 运行的上下文数据（例如用户名/uid 或其他用户信息）
- 依赖项（例如 logger 对象、数据获取器等）
- 辅助函数

!!! danger "注意"

    上下文对象**不会**发送给 LLM。它纯粹是一个本地对象，你可以从中读取、向其中写入并调用其方法。

在一次运行中，派生包装器共享相同的底层应用上下文、审批状态和用量追踪。嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行可能会附加不同的 `tool_input`，但默认情况下不会获得应用状态的隔离副本。

### `RunContextWrapper` 提供的内容

[`RunContextWrapper`][agents.run_context.RunContextWrapper] 是你应用自定义上下文对象的包装器。实际中你最常使用的是：

- [`wrapper.context`][agents.run_context.RunContextWrapper.context]：用于你自己的可变应用状态和依赖。
- [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]：用于当前运行中的聚合请求与 token 用量。
- [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]：用于当前运行在 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内执行时的结构化输入。
- [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]：当你需要以编程方式更新审批状态时使用。

只有 `wrapper.context` 是你应用自定义的对象。其他字段都是由 SDK 管理的运行时元数据。

如果你之后为了 human-in-the-loop 或持久化任务工作流序列化 [`RunState`][agents.run_state.RunState]，这些运行时元数据会随状态一同保存。如果你打算持久化或传输序列化状态，请避免在 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] 中放置敏感信息。

会话状态是另一个独立问题。请根据你希望如何延续轮次，使用 `result.to_input_list()`、`session`、`conversation_id` 或 `previous_response_id`。相关决策请参见 [results](results.md)、[running agents](running_agents.md) 和 [sessions](sessions/index.md)。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. 这是上下文对象。这里我们使用了 dataclass，但你可以使用任何类型。
2. 这是一个工具。你可以看到它接收 `RunContextWrapper[UserInfo]`。工具实现会从上下文中读取数据。
3. 我们将智能体标注为泛型 `UserInfo`，这样类型检查器就能捕获错误（例如，如果我们尝试传入接收不同上下文类型的工具）。
4. 上下文会传给 `run` 函数。
5. 智能体会正确调用工具并获取年龄。

---

### 高级内容：`ToolContext`

在某些情况下，你可能希望访问正在执行的工具的额外元数据——例如其名称、调用 ID 或原始参数字符串。  
为此，你可以使用 [`ToolContext`][agents.tool_context.ToolContext] 类，它扩展了 `RunContextWrapper`。

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent, Runner, function_tool
from agents.tool_context import ToolContext

class WeatherContext(BaseModel):
    user_id: str

class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")

@function_tool
def get_weather(ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]) -> Weather:
    print(f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext` 提供与 `RunContextWrapper` 相同的 `.context` 属性，  
并额外提供当前工具调用特有的字段：

- `tool_name` – 正在调用的工具名称  
- `tool_call_id` – 此工具调用的唯一标识符  
- `tool_arguments` – 传给工具的原始参数字符串  
- `tool_namespace` – 工具调用对应的 Responses 命名空间（当工具通过 `tool_namespace()` 或其他带命名空间的表面加载时）  
- `qualified_tool_name` – 在可用时，带命名空间限定的工具名称  

当你在执行期间需要工具级元数据时，使用 `ToolContext`。  
对于智能体与工具之间的一般上下文共享，`RunContextWrapper` 仍然足够。由于 `ToolContext` 扩展自 `RunContextWrapper`，当嵌套的 `Agent.as_tool()` 运行提供了结构化输入时，它也可以暴露 `.tool_input`。

---

## 智能体/LLM 上下文

调用 LLM 时，它**唯一**能看到的数据来自对话历史。这意味着如果你想让 LLM 能看到某些新数据，就必须以某种方式让其出现在该历史中。可用方式有以下几种：

1. 你可以将其加入智能体的 `instructions`。这也称为“系统提示词”或“开发者消息”。系统提示可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。这是对始终有用的信息的常见策略（例如用户名或当前日期）。
2. 在调用 `Runner.run` 函数时将其加入 `input`。这与 `instructions` 策略类似，但允许你把消息放在 [指令链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 中更低的位置。
3. 通过工具调用暴露它。这适用于_按需_上下文——LLM 决定何时需要某些数据，并可调用工具获取该数据。
4. 使用检索或网络检索。这些是能够从文件或数据库（检索）或网络（网络检索）获取相关数据的特殊工具。这有助于让回复基于相关上下文数据进行“锚定”。