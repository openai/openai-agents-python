---
search:
  exclude: true
---
# 上下文管理

“上下文”一词含义宽泛。你可能关心两大类上下文：

1. 代码本地可用的上下文：这是工具函数运行时、`on_handoff` 等回调、生命周期钩子等可能需要的数据和依赖。
2. LLM 可用的上下文：这是 LLM 在生成响应时能看到的数据。

## 本地上下文

这通过 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类及其内部的 [`context`][agents.run_context.RunContextWrapper.context] 属性来表示。其工作方式如下：

1. 你创建任意 Python 对象即可。常见模式是使用 dataclass 或 Pydantic 对象。
2. 将该对象传入各种运行方法（例如 `Runner.run(..., **context=whatever**)`）。
3. 你所有的工具调用、生命周期钩子等都会收到一个包装对象 `RunContextWrapper[T]`，其中 `T` 表示你的上下文对象类型，你可以通过 `wrapper.context` 访问它。

需要注意的**最重要**一点：给定一次智能体运行中的每个智能体、工具函数、生命周期等都必须使用相同类型的上下文。

你可以将上下文用于以下场景：

-   运行所需的上下文数据（例如用户名/uid 或关于用户的其他信息）
-   依赖项（例如日志记录器、数据获取器等）
-   帮助函数

!!! danger "注意"

    上下文对象**不**会发送给 LLM。它纯粹是一个本地对象，你可以读取、写入并在其上调用方法。

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

1. 这是上下文对象。此处使用了 dataclass，但你可以使用任何类型。
2. 这是一个工具。你可以看到它接收 `RunContextWrapper[UserInfo]`。工具实现会从上下文中读取。
3. 我们用泛型 `UserInfo` 标注智能体，以便类型检查器能捕获错误（例如，如果我们尝试传入一个期望不同上下文类型的工具）。
4. 将上下文传递给 `run` 函数。
5. 智能体正确调用工具并获取年龄。

## 智能体/LLM 上下文

当调用 LLM 时，它能看到的**唯一**数据来自对话历史。这意味着，如果你想让一些新数据对 LLM 可用，必须以能将其纳入该历史的方式进行。常见做法包括：

1. 将其添加到智能体的 `instructions`。这也称为“系统提示词”或“开发者消息”。系统提示词可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。这对始终有用的信息很常见（例如用户名或当前日期）。
2. 在调用 `Runner.run` 函数时，将其添加到 `input`。这与 `instructions` 策略类似，但允许你添加在[指挥链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)中级别更低的消息。
3. 通过 工具调用 暴露它。这对“按需”的上下文很有用——LLM 决定何时需要某些数据，并可调用该工具来获取数据。
4. 使用 文件检索 或 网络检索。它们是能够从文件或数据库（文件检索）或从网络（网络检索）中获取相关数据的特殊工具。这有助于用相关的上下文数据“奠基（ground）”响应。