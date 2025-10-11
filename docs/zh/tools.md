---
search:
  exclude: true
---
# 工具

工具让智能体能够执行操作：比如获取数据、运行代码、调用外部API，甚至使用计算机。Agent SDK中有三类工具：

-   托管工具：这些工具在AI模型旁边的LLM服务器上运行。OpenAI提供检索、网络搜索和计算机使用作为托管工具。
-   函数调用：这些允许你将任何Python函数作为工具使用。
-   智能体作为工具：这允许你将智能体作为工具使用，让智能体可以调用其他智能体而不需要交接控制权。

## 托管工具

使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI提供了一些内置工具：

- [`WebSearchTool`][agents.tool.WebSearchTool] 让智能体搜索网络。
- [`FileSearchTool`][agents.tool.FileSearchTool] 允许从你的OpenAI向量存储中检索信息。
- [`ComputerTool`][agents.tool.ComputerTool] 允许自动化计算机使用任务。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 让LLM在沙盒环境中执行代码。
- [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程MCP服务器的工具暴露给模型。
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示生成图像。
- [`LocalShellTool`][agents.tool.LocalShellTool] 在你的机器上运行shell命令。

```python
from agents import Agent, FileSearchTool, Runner, WebSearchTool

agent = Agent(
    name="Assistant",
    tools=[
        WebSearchTool(),
        FileSearchTool(
            max_num_results=3,
            vector_store_ids=["VECTOR_STORE_ID"],
        ),
    ],
)

async def main():
    result = await Runner.run(agent, "Which coffee shop should I go to, taking into account my preferences and the weather today in SF?")
    print(result.final_output)
```

## 函数工具

你可以使用任何Python函数作为工具。Agents SDK会自动设置工具：

- 工具的名称将是Python函数的名称（或者你可以提供一个名称）
- 工具描述将从函数的文档字符串中获取（或者你可以提供一个描述）
- 函数输入的模式会自动从函数的参数创建
- 每个输入的描述从函数的文档字符串中获取，除非被禁用

我们使用Python的 `inspect` 模块来提取函数签名，同时使用 [`griffe`](https://mkdocstrings.github.io/griffe/) 来解析文档字符串，使用 `pydantic` 来创建模式。

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper, function_tool


class Location(TypedDict):
    lat: float
    long: float

@function_tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@function_tool(name_override="fetch_data")  # (3)!
def read_file(ctx: RunContextWrapper[Any], path: str, directory: str | None = None) -> str:
    """Read the contents of a file.

    Args:
        path: The path to the file to read.
        directory: The directory to read the file from.
    """
    # In real life, we'd read the file from the file system
    return "<file contents>"


agent = Agent(
    name="Assistant",
    tools=[fetch_weather, read_file],  # (4)!
)

for tool in agent.tools:
    if isinstance(tool, FunctionTool):
        print(tool.name)
        print(tool.description)
        print(json.dumps(tool.params_json_schema, indent=2))
        print()

```

1.  你可以使用任何Python类型作为函数的参数，函数可以是同步或异步的。
2.  如果存在文档字符串，它们会被用来捕获描述和参数描述
3.  函数可以选择性地接收 `context`（必须是第一个参数）。你也可以设置覆盖项，比如工具的名称、描述、使用哪种文档字符串风格等。
4.  你可以将装饰后的函数传递给工具列表。

??? note "展开查看输出"

    ```
    fetch_weather
    Fetch the weather for a given location.
    {
    "$defs": {
      "Location": {
        "properties": {
          "lat": {
            "title": "Lat",
            "type": "number"
          },
          "long": {
            "title": "Long",
            "type": "number"
          }
        },
        "required": [
          "lat",
          "long"
        ],
        "title": "Location",
        "type": "object"
      }
    },
    "properties": {
      "location": {
        "$ref": "#/$defs/Location",
        "description": "The location to fetch the weather for."
      }
    },
    "required": [
      "location"
    ],
    "title": "fetch_weather_args",
    "type": "object"
    }

    fetch_data
    Read the contents of a file.
    {
    "properties": {
      "path": {
        "description": "The path to the file to read.",
        "title": "Path",
        "type": "string"
      },
      "directory": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "The directory to read the file from.",
        "title": "Directory"
      }
    },
    "required": [
      "path"
    ],
    "title": "fetch_data_args",
    "type": "object"
    }
    ```

### 自定义函数工具

有时候，你不想使用Python函数作为工具。如果你愿意，可以直接创建一个 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

-   `name`
-   `description`
-   `params_json_schema`，这是参数的JSON模式
-   `on_invoke_tool`，这是一个异步函数，接收 [`ToolContext`][agents.tool_context.ToolContext] 和作为JSON字符串的参数，并且必须返回作为字符串的工具输出。

```python
from typing import Any

from pydantic import BaseModel

from agents import RunContextWrapper, FunctionTool



def do_some_work(data: str) -> str:
    return "done"


class FunctionArgs(BaseModel):
    username: str
    age: int


async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
    parsed = FunctionArgs.model_validate_json(args)
    return do_some_work(data=f"{parsed.username} is {parsed.age} years old")


tool = FunctionTool(
    name="process_user",
    description="Processes extracted user data",
    params_json_schema=FunctionArgs.model_json_schema(),
    on_invoke_tool=run_function,
)
```

### 自动参数和文档字符串解析

如前所述，我们自动解析函数签名来提取工具的模式，并且解析文档字符串来提取工具的描述和各个参数的描述。一些注意事项：

1. 签名解析通过 `inspect` 模块完成。我们使用类型注解来理解参数的类型，并动态构建一个Pydantic模型来表示整体模式。它支持大多数类型，包括Python原语、Pydantic模型、TypedDict等。
2. 我们使用 `griffe` 来解析文档字符串。支持的文档字符串格式有 `google`、`sphinx` 和 `numpy`。我们尝试自动检测文档字符串格式，但这是尽力而为的，你可以在调用 `function_tool` 时显式设置它。你也可以通过设置 `use_docstring_info` 为 `False` 来禁用文档字符串解析。

模式提取的代码位于 [`agents.function_schema`][] 中。

## 智能体作为工具

在某些工作流中，你可能希望一个中央智能体来协调一组专门的智能体网络，而不是交接控制权。你可以通过将智能体建模为工具来实现这一点。

```python
from agents import Agent, Runner
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You translate the user's message to Spanish",
)

french_agent = Agent(
    name="French agent",
    instructions="You translate the user's message to French",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate."
        "If asked for multiple translations, you call the relevant tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
    ],
)

async def main():
    result = await Runner.run(orchestrator_agent, input="Say 'Hello, how are you?' in Spanish.")
    print(result.final_output)
```

### 自定义工具智能体

`agent.as_tool` 函数是一个方便的方法，使得将智能体转换为工具变得容易。然而，它不支持所有配置；例如，你不能设置 `max_turns`。对于高级用例，直接在工具实现中使用 `Runner.run`：

```python
@function_tool
async def run_my_agent() -> str:
    """A tool that runs the agent with custom configs"""

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(
        agent,
        input="...",
        max_turns=5,
        run_config=...
    )

    return str(result.final_output)
```

### 自定义输出提取

在某些情况下，你可能希望在将工具智能体的输出返回给中央智能体之前修改它。这可能有用，如果你想：

- 从子智能体的聊天记录中提取特定信息（例如，JSON有效载荷）。
- 转换或重新格式化智能体的最终答案（例如，将Markdown转换为纯文本或CSV）。
- 验证输出或在智能体的响应缺失或格式错误时提供回退值。

你可以通过向 `as_tool` 方法提供 `custom_output_extractor` 参数来做到这一点：

```python
async def extract_json_payload(run_result: RunResult) -> str:
    # 以相反的顺序扫描智能体的输出，直到我们找到来自工具调用的类似JSON的消息。
    for item in reversed(run_result.new_items):
        if isinstance(item, ToolCallOutputItem) and item.output.strip().startswith("{"):
            return item.output.strip()
    # 如果什么都没找到，回退到空JSON对象
    return "{}"


json_tool = data_agent.as_tool(
    tool_name="get_data_json",
    tool_description="Run the data agent and return only its JSON payload",
    custom_output_extractor=extract_json_payload,
)
```

### 条件工具启用

你可以使用 `is_enabled` 参数在运行时条件性地启用或禁用智能体工具。这允许你根据上下文、用户偏好或运行时条件动态过滤哪些工具可供LLM使用。

```python
import asyncio
from agents import Agent, AgentBase, Runner, RunContextWrapper
from pydantic import BaseModel

class LanguageContext(BaseModel):
    language_preference: str = "french_spanish"

def french_enabled(ctx: RunContextWrapper[LanguageContext], agent: AgentBase) -> bool:
    """Enable French for French+Spanish preference."""
    return ctx.context.language_preference == "french_spanish"

# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish.",
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_enabled,
        ),
    ],
)

async def main():
    context = RunContextWrapper(LanguageContext(language_preference="french_spanish"))
    result = await Runner.run(orchestrator, "How are you?", context=context.context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` 参数接受：

- **布尔值**：`True`（始终启用）或 `False`（始终禁用）
- **可调用函数**：接收 `(context, agent)` 并返回布尔值的函数
- **异步函数**：用于复杂条件逻辑的异步函数

禁用的工具在运行时对LLM完全隐藏，这使得它适用于：

- 基于用户权限的功能开关
- 环境特定的工具可用性（开发环境 vs 生产环境）
- A/B测试不同的工具配置
- 基于运行时状态的动态工具过滤

## 函数工具中的错误处理

当你通过 `@function_tool` 创建函数工具时，你可以传递一个 `failure_error_function`。这是一个函数，在工具调用崩溃的情况下向LLM提供错误响应。

- 默认情况下（即如果你没有传递任何东西），它会运行一个 `default_tool_error_function`，告诉LLM发生了错误。
- 如果你传递自己的错误函数，它会运行那个函数，并将响应发送给LLM。
- 如果你显式地传递 `None`，那么任何工具调用错误都会被重新抛出，供你处理。这可能是 `ModelBehaviorError`（如果模型产生了无效的JSON），或者 `UserError`（如果你的代码崩溃了）等。

```python
from agents import function_tool, RunContextWrapper
from typing import Any

def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."

@function_tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
     This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(f"Could not retrieve profile for user_id: {user_id}. API returned an error.")

```

如果你手动创建一个 `FunctionTool` 对象，那么你必须在 `on_invoke_tool` 函数内部处理错误。