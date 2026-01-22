---
search:
  exclude: true
---
# 上下文管理

上下文（Context）是一个含义很宽泛的术语。你可能会关注两类主要的上下文：

1. 你的代码在本地可用的上下文：这是在工具函数运行时、`on_handoff` 等回调期间、生命周期钩子中等场景可能需要的数据与依赖。
2. LLM 可用的上下文：这是 LLM 在生成响应时看到的数据。

## 本地上下文

这通过 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类以及其中的 [`context`][agents.run_context.RunContextWrapper.context] 属性来表示。其工作方式如下：

1. 你创建任意你想要的 Python 对象。常见模式是使用 dataclass 或 Pydantic 对象。
2. 你将该对象传给各种 run 方法（例如 `Runner.run(..., context=whatever)`）。
3. 你所有的工具调用、生命周期钩子等都会收到一个包装对象 `RunContextWrapper[T]`，其中 `T` 表示你的上下文对象类型；你可以通过 `wrapper.context` 访问它。

需要注意的**最重要**的一点是：对于一次给定的智能体运行，该运行中的每个智能体、工具函数、生命周期等都必须使用相同的上下文 _类型_。

你可以用上下文来做这些事情，例如：

-   运行的上下文数据（例如用户名/uid 或其他与用户相关的信息）
-   依赖（例如 logger 对象、数据获取器等）
-   辅助函数

!!! danger "注意"

    上下文对象**不会**发送给 LLM。它完全是一个本地对象，你可以从中读取、向其中写入并调用其方法。

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
2. 这是一个工具。你可以看到它接收 `RunContextWrapper[UserInfo]`。该工具实现会从上下文中读取数据。
3. 我们用泛型 `UserInfo` 标注该智能体，这样类型检查器就能捕获错误（例如，如果我们尝试传入一个接收不同上下文类型的工具）。
4. 上下文被传入 `run` 函数。
5. 该智能体正确调用工具并获取年龄。

---

### 高级：`ToolContext`

在某些情况下，你可能想访问正在执行的工具的额外元数据——例如工具名、调用 ID，或原始参数字符串。  
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

- `tool_name` – 被调用工具的名称  
- `tool_call_id` – 此次工具调用的唯一标识符  
- `tool_arguments` – 传给工具的原始参数字符串  

当你在执行期间需要工具级元数据时，使用 `ToolContext`。  
对于智能体与工具之间的一般上下文共享，`RunContextWrapper` 仍然足够。

---

## 智能体/LLM 上下文

当调用 LLM 时，它能看到的**唯一**数据来自对话历史。这意味着，如果你想让一些新数据对 LLM 可用，你必须以一种能让它出现在历史中的方式来做。实现方式有几种：

1. 你可以把它加入智能体的 `instructions`。这也被称为 “system prompt” 或 “developer message”。系统提示词可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。这是针对始终有用的信息的常见策略（例如用户的名字或当前日期）。
2. 在调用 `Runner.run` 函数时，把它加入 `input`。这与 `instructions` 的策略类似，但允许你的消息在[指令优先级链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)中处于更低位置。
3. 通过工具调用暴露它。这适用于 _按需_ 上下文——LLM 决定何时需要某些数据，并可以调用工具来获取这些数据。
4. 使用检索或网络检索。这些是特殊工具，能够从文件或数据库（检索）或从网络（网络检索）中获取相关数据。这有助于将响应“落地”到相关的上下文数据之上。