---
search:
  exclude: true
---
# 工具

工具让智能体能够执行操作：如获取数据、运行代码、调用外部 API，甚至进行计算机操作。在 Agents SDK 中，工具分为三类：

- 由OpenAI托管的工具：这些在 LLM 服务 旁与 AI 模型一起运行。OpenAI 提供 retrieval、网络检索 和 计算机操作 作为托管工具。
- Function Calling：这些允许你将任意 Python 函数用作工具。
- 智能体作为工具：这允许你将一个智能体作为工具，从而让智能体调用其他智能体，而无需进行任务转移。

## 由OpenAI托管的工具

使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI 提供了一些内置工具：

- [`WebSearchTool`][agents.tool.WebSearchTool] 让智能体进行网络检索。
- [`FileSearchTool`][agents.tool.FileSearchTool] 支持从你的 OpenAI 向量存储 中检索信息。
- [`ComputerTool`][agents.tool.ComputerTool] 支持自动化的计算机操作任务。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 让 LLM 在沙箱环境中执行代码。
- [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程 MCP 服务 的工具暴露给模型。
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示生成图像。
- [`LocalShellTool`][agents.tool.LocalShellTool] 在你的机器上运行 shell 命令。

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

## 工具调用

你可以将任意 Python 函数作为工具使用。Agents SDK 会自动完成工具设置：

- 工具名将采用该 Python 函数的名称（或你可以自定义名称）
- 工具描述将来自函数的 docstring（或你可以自定义描述）
- 函数输入的 schema 会从函数的参数自动创建
- 各输入的描述会从函数的 docstring 中提取，除非你禁用了该行为

我们使用 Python 的 `inspect` 模块提取函数签名，使用 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析 docstring，并使用 `pydantic` 来创建 schema。

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

1. 你可以使用任意 Python 类型作为函数参数，函数可以是同步或异步。
2. 若存在 docstring，将用于捕获描述和参数说明。
3. 函数可选接收 `context`（必须为第一个参数）。你也可以设置覆盖项，如工具名称、描述、docstring 风格等。
4. 你可以将装饰后的函数传入工具列表。

??? note "展开以查看输出"

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

### 从工具调用返回图像或文件

除了返回文本输出外，你还可以将一个或多个图像或文件作为工具调用的输出。为此，你可以返回以下任意类型：

- 图像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或其 TypedDict 版本，[`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
- 文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或其 TypedDict 版本，[`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
- 文本：字符串或可字符串化对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或其 TypedDict 版本，[`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义工具调用

有时你并不想使用 Python 函数作为工具。如果你愿意，可以直接创建一个 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

- `name`
- `description`
- `params_json_schema`，即参数的 JSON schema
- `on_invoke_tool`，一个异步函数，它接收 [`ToolContext`][agents.tool_context.ToolContext] 和作为 JSON 字符串的参数，并且必须以字符串形式返回工具输出。

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

### 自动解析参数与文档字符串

如前所述，我们会自动解析函数签名以提取工具的 schema，并解析 docstring 以提取工具和各参数的描述。注意事项如下：

1. 使用 `inspect` 模块进行签名解析。我们利用类型注解了解参数类型，并动态构建一个 Pydantic 模型来表示整体 schema。它支持大多数类型，包括 Python 基本类型、Pydantic 模型、TypedDict 等。
2. 我们使用 `griffe` 解析 docstring。支持的 docstring 格式包括 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测 docstring 格式，但这只是尽力而为；你可以在调用 `function_tool` 时显式设置。你也可以通过将 `use_docstring_info` 设为 `False` 来禁用 docstring 解析。

用于 schema 提取的代码位于 [`agents.function_schema`][] 中。

## 智能体作为工具

在某些工作流中，你可能希望由一个中心智能体来编排一组专业化智能体，而不是进行控制权的任务转移。你可以通过将智能体建模为工具来实现这一点。

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

### 工具化智能体自定义

`agent.as_tool` 函数是一个便捷方法，便于将智能体转换为工具。但它并不支持所有配置；例如，你无法设置 `max_turns`。对于高级用例，请在你的工具实现中直接使用 `Runner.run`：

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

在某些情况下，你可能希望在将工具化智能体的输出返回给中心智能体之前进行修改。这在以下情形很有用：

- 从子智能体的对话历史中提取特定信息（例如 JSON 负载）。
- 转换或重新格式化智能体的最终答案（例如将 Markdown 转换为纯文本或 CSV）。
- 验证输出，或在智能体响应缺失或格式错误时提供回退值。

你可以通过向 `as_tool` 方法提供 `custom_output_extractor` 参数来实现：

```python
async def extract_json_payload(run_result: RunResult) -> str:
    # Scan the agent’s outputs in reverse order until we find a JSON-like message from a tool call.
    for item in reversed(run_result.new_items):
        if isinstance(item, ToolCallOutputItem) and item.output.strip().startswith("{"):
            return item.output.strip()
    # Fallback to an empty JSON object if nothing was found
    return "{}"


json_tool = data_agent.as_tool(
    tool_name="get_data_json",
    tool_description="Run the data agent and return only its JSON payload",
    custom_output_extractor=extract_json_payload,
)
```

### 条件式启用工具

你可以在运行时使用 `is_enabled` 参数有条件地启用或禁用智能体工具。这允许你基于上下文、用户偏好或运行时条件，动态筛选 LLM 可用的工具。

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

被禁用的工具在运行时对 LLM 完全不可见，适用于：

- 基于用户权限的功能开关
- 特定环境的工具可用性（dev 与 prod）
- 针对不同工具配置的 A/B 测试
- 基于运行时状态的动态工具过滤

## 在工具调用中处理错误

当你通过 `@function_tool` 创建工具调用时，你可以传入 `failure_error_function`。这是一个在工具调用崩溃时向 LLM 提供错误响应的函数。

- 默认情况下（即你未传入任何内容），会运行 `default_tool_error_function`，告知 LLM 发生了错误。
- 如果你传入了自定义错误函数，就会执行该函数，并将其响应发送给 LLM。
- 如果你显式传入 `None`，则任何工具调用错误都会被重新抛出以供你处理。若模型生成了无效 JSON，则可能是 `ModelBehaviorError`；若你的代码崩溃，则可能是 `UserError`，等等。

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

如果你是手动创建 `FunctionTool` 对象，则必须在 `on_invoke_tool` 函数内部处理错误。