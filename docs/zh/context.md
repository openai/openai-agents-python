---
search:
  exclude: true
---
# 上下文管理

“上下文”是一个含义较多的术语。通常你会关心两类上下文：

1. 代码本地可用的上下文：当工具函数运行、在 `on_handoff` 这类回调、生命周期钩子等期间可能需要的数据与依赖。
2. LLM 可用的上下文：LLM 在生成响应时可见的数据。

## 本地上下文

这由 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类以及其中的 [`context`][agents.run_context.RunContextWrapper.context] 属性来表示。其工作方式是：

1. 你创建任意 Python 对象。常见模式是使用数据类（dataclass）或 Pydantic 对象。
2. 将该对象传给各类运行方法（例如 `Runner.run(..., **context=whatever**)`）。
3. 你的所有工具调用、生命周期钩子等都会接收一个包装对象 `RunContextWrapper[T]`，其中 `T` 表示你的上下文对象类型，你可通过 `wrapper.context` 访问。

需要特别注意的**最重要**一点：给定一次智能体运行，所有智能体、工具函数、生命周期等都必须使用相同_类型_的上下文。

你可以将上下文用于：

- 运行的情境数据（例如用户名/uid 或关于该用户的其他信息）
- 依赖（例如日志记录器对象、数据获取器等）
- 帮助函数

!!! danger "注意"

    上下文对象**不会**发送给 LLM。它纯粹是一个本地对象，你可以读取、写入并在其上调用方法。

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

1. 这是上下文对象。这里使用了数据类，但你可以使用任意类型。
2. 这是一个工具。它接收 `RunContextWrapper[UserInfo]`。工具实现会从上下文中读取数据。
3. 我们用泛型 `UserInfo` 标注该智能体，以便类型检查器能捕获错误（例如，如果我们尝试传入一个接收不同上下文类型的工具）。
4. 上下文被传入 `run` 函数。
5. 智能体正确调用工具并获取年龄。

---

### 进阶：`ToolContext`

在某些情况下，你可能希望访问正在执行的工具的额外元数据——例如其名称、调用 ID 或原始参数字符串。  
为此，你可以使用扩展自 `RunContextWrapper` 的 [`ToolContext`][agents.tool_context.ToolContext] 类。

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
并额外包含当前工具调用特有的字段：

- `tool_name` – 正在调用的工具名称  
- `tool_call_id` – 此次工具调用的唯一标识符  
- `tool_arguments` – 传递给工具的原始参数字符串  

当你在执行期间需要工具层面的元数据时，请使用 `ToolContext`。  
对于在智能体与工具之间共享的一般上下文，`RunContextWrapper` 已经足够。

---

## 智能体/LLM 上下文

当调用 LLM 时，它能看到的**唯一**数据来自对话历史。也就是说，如果你想让 LLM 获取新的数据，必须以能让该数据进入对话历史的方式提供它。常见做法包括：

1. 将其添加到智能体的 `instructions`。这也被称为“system prompt”或“developer message”。System prompts 可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。对于总是有用的信息（例如用户名或当前日期），这是常用策略。
2. 在调用 `Runner.run` 时将其添加到 `input`。这与 `instructions` 的策略类似，但允许消息位于[指挥链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)的更低位置。
3. 通过工具调用（function tools）暴露它。适用于_按需_上下文——LLM 决定何时需要某些数据，并可调用工具以获取该数据。
4. 使用检索或网络检索（web search）。这些是特殊工具，能够从文件或数据库中获取相关数据（检索），或从网络中获取（网络检索）。这有助于使响应基于相关的上下文数据。