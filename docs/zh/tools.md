---
search:
  exclude: true
---
# 工具

工具让智能体采取行动：例如获取数据、运行代码、调用外部 API，甚至进行计算机操作。Agents SDK 中有三类工具：

- 托管工具：这些工具与 AI 模型一同运行在 LLM 服务上。OpenAI 提供检索、网络检索与计算机操作等托管工具。
- 函数调用：可以将任意 Python 函数用作工具。
- 将智能体作为工具：可以把一个智能体当作工具使用，使智能体在不进行任务转移的情况下调用其他智能体。

## 托管工具

在使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI 提供一些内置工具：

- [`WebSearchTool`][agents.tool.WebSearchTool] 允许智能体进行网络检索。
- [`FileSearchTool`][agents.tool.FileSearchTool] 允许从你的 OpenAI 向量存储中检索信息。
- [`ComputerTool`][agents.tool.ComputerTool] 支持自动化计算机操作任务。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 允许 LLM 在沙箱环境中执行代码。
- [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程 MCP 服务的工具暴露给模型。
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

- 工具名称将取自 Python 函数名（也可以手动指定）
- 工具描述将取自函数的 docstring（也可以手动提供）
- 函数输入的 schema 会根据函数参数自动创建
- 各输入参数的描述默认取自函数的 docstring，可关闭

我们使用 Python 的 `inspect` 模块提取函数签名，使用 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析 docstring，并用 `pydantic` 创建 schema。

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

1. 你可以为函数参数使用任意 Python 类型，函数可为同步或异步。
2. 若存在 docstring，则用于提取工具描述与参数描述。
3. 函数可选接收 `context`（必须为第一个参数）。你也可以设置一些覆盖项，如工具名称、描述、docstring 风格等。
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

除了返回文本输出外，你还可以将一张或多张图像或文件作为工具调用的输出返回。可返回以下任意类型：

- 图像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或 TypedDict 版本 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
- 文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或 TypedDict 版本 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
- 文本：字符串或可转为字符串的对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或 TypedDict 版本 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义工具调用

有时你可能不希望使用 Python 函数作为工具。可以直接创建一个 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

- `name`
- `description`
- `params_json_schema`，即参数的 JSON schema
- `on_invoke_tool`，一个异步函数，接收 [`ToolContext`][agents.tool_context.ToolContext] 与 JSON 字符串形式的参数，并且必须以字符串形式返回工具输出。

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

### 参数与 docstring 的自动解析

如前所述，我们会自动解析函数签名以提取工具的 schema，并解析 docstring 以提取工具及各参数的描述。注意事项：

1. 使用 `inspect` 模块解析签名。我们利用类型注解理解参数类型，并动态构建 Pydantic 模型来表示整体 schema。支持大多数类型，包括 Python 基本类型、Pydantic 模型、TypedDicts 等。
2. 我们使用 `griffe` 解析 docstring。支持的 docstring 格式包括 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测 docstring 格式，但这是尽力而为，你也可以在调用 `function_tool` 时显式设置。你还可以通过将 `use_docstring_info` 设为 `False` 来禁用 docstring 解析。

用于 schema 提取的代码位于 [`agents.function_schema`][]。

## 将智能体作为工具

在某些工作流中，你可能希望由一个中心智能体编排一组专门化智能体，而不是进行任务转移。你可以通过将智能体建模为工具来实现。

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

### 自定义工具化智能体

`agent.as_tool` 是一个便捷方法，可轻松将智能体转为工具。但它不支持所有配置；例如，你无法设置 `max_turns`。对于高级用例，请在你的工具实现中直接使用 `Runner.run`：

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

### 自定义输出抽取

在某些情况下，你可能希望在将结果返回给中心智能体之前修改工具智能体的输出。如果你希望：

- 从子智能体的对话历史中抽取特定信息（例如 JSON 负载）
- 转换或重新格式化智能体的最终答案（例如将 Markdown 转为纯文本或 CSV）
- 验证输出，或在智能体响应缺失或格式错误时提供回退值

你可以在调用 `as_tool` 时传入 `custom_output_extractor` 参数来实现：

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

你可以在运行时使用 `is_enabled` 参数有条件地启用或禁用智能体工具。这样可以根据上下文、用户偏好或运行时条件动态筛选对 LLM 可用的工具。

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

`is_enabled` 参数可接收：

- **布尔值**：`True`（始终启用）或 `False`（始终禁用）
- **可调用函数**：接收 `(context, agent)` 并返回布尔值的函数
- **异步函数**：用于更复杂的条件逻辑

被禁用的工具在运行时对 LLM 完全不可见，适用于：

- 基于用户权限的功能开关
- 区分环境的工具可用性（开发 vs 生产）
- 不同工具配置的 A/B 测试
- 基于运行时状态的动态工具筛选

## 在工具调用中处理错误

当你通过 `@function_tool` 创建工具时，可以传入 `failure_error_function`。这是在工具调用崩溃时向 LLM 提供错误响应的函数。

- 默认情况下（即未传入时），会运行 `default_tool_error_function`，告知 LLM 发生了错误。
- 如果传入你自己的错误处理函数，则会运行它，并将其响应发送给 LLM。
- 如果显式传入 `None`，则任何工具调用错误都会重新抛出供你处理。若模型生成了无效 JSON，可能是 `ModelBehaviorError`；若你的代码崩溃，可能是 `UserError`，等等。

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

如果你手动创建 `FunctionTool` 对象，则必须在 `on_invoke_tool` 函数内部处理错误。