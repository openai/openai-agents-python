---
search:
  exclude: true
---
# 工具

工具让智能体能够采取行动：例如获取数据、运行代码、调用外部 API，甚至进行计算机操作。该 SDK 支持五种目录：

- 由OpenAI托管的工具：与模型一起在 OpenAI 服务 上运行。
- 本地运行时工具：在你的环境中运行（计算机操作、shell、apply patch）。
- Function calling：将任意 Python 函数包装为工具。
- 将智能体作为工具：将一个智能体暴露为可调用的工具，而无需完整的任务转移。
- 实验性：Codex 工具：从工具调用中运行以工作区为范围的 Codex 任务。

## 托管工具

在使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI 提供了一些内置工具：

- [`WebSearchTool`][agents.tool.WebSearchTool] 允许智能体进行网络检索。
- [`FileSearchTool`][agents.tool.FileSearchTool] 允许从你的 OpenAI 向量存储 中检索信息。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 让 LLM 能在沙箱环境中执行代码。
- [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程 MCP 服务 的工具暴露给模型。
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示生成图片。

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

## 本地运行时工具

本地运行时工具在你的环境中执行，需要你提供实现：

- [`ComputerTool`][agents.tool.ComputerTool]：实现 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 接口，以启用 GUI/浏览器自动化。
- [`ShellTool`][agents.tool.ShellTool] 或 [`LocalShellTool`][agents.tool.LocalShellTool]：提供一个 shell 执行器来运行命令。
- [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：实现 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] 以在本地应用 diff。

```python
from agents import Agent, ApplyPatchTool, ShellTool
from agents.computer import AsyncComputer
from agents.editor import ApplyPatchResult, ApplyPatchOperation, ApplyPatchEditor


class NoopComputer(AsyncComputer):
    environment = "browser"
    dimensions = (1024, 768)
    async def screenshot(self): return ""
    async def click(self, x, y, button): ...
    async def double_click(self, x, y): ...
    async def scroll(self, x, y, scroll_x, scroll_y): ...
    async def type(self, text): ...
    async def wait(self): ...
    async def move(self, x, y): ...
    async def keypress(self, keys): ...
    async def drag(self, path): ...


class NoopEditor(ApplyPatchEditor):
    async def create_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def update_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def delete_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")


async def run_shell(request):
    return "shell output"


agent = Agent(
    name="Local tools agent",
    tools=[
        ShellTool(executor=run_shell),
        ApplyPatchTool(editor=NoopEditor()),
        # ComputerTool expects a Computer/AsyncComputer implementation; omitted here for brevity.
    ],
)
```

## 函数工具

你可以将任意 Python 函数用作工具。Agents SDK 会自动完成工具设置：

- 工具名称将是该 Python 函数的名称（或你可以自定义名称）
- 工具描述将取自函数的 docstring（或你可以提供描述）
- 函数输入的 schema 会根据函数参数自动创建
- 每个输入的描述将取自函数的 docstring，除非禁用

我们使用 Python 的 `inspect` 模块提取函数签名，配合 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析 docstring，并使用 `pydantic` 创建 schema。

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

1. 你可以在函数参数中使用任意 Python 类型，函数可以是同步或异步。
2. 若存在 docstring，将用于捕获描述和参数说明。
3. 函数可选接收 `context`（必须是第一个参数）。你也可以设置覆盖项，如工具名称、描述、docstring 风格等。
4. 你可以将已装饰的函数传入工具列表。

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

### 从函数工具返回图片或文件

除了返回文本输出外，你还可以将一张或多张图片或文件作为函数工具的输出。为此，你可以返回以下任意一种：

- 图片：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或 TypedDict 版本 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
- 文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或 TypedDict 版本 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
- 文本：字符串或可转为字符串的对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或 TypedDict 版本 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义函数工具

有时，你并不想使用 Python 函数作为工具。此时你可以直接创建一个 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

- `name`
- `description`
- `params_json_schema`，即参数的 JSON schema
- `on_invoke_tool`，一个异步函数，接收 [`ToolContext`][agents.tool_context.ToolContext] 和以 JSON 字符串表示的参数，并且必须返回字符串形式的工具输出。

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

### 自动参数与 docstring 解析

如前所述，我们会自动解析函数签名以提取工具的 schema，并解析 docstring 以提取工具和各个参数的描述。注意事项如下：

1. 使用 `inspect` 模块进行签名解析。我们使用类型注解理解参数类型，并动态构建一个 Pydantic 模型来表示整体 schema。它支持大多数类型，包括 Python 基本类型、Pydantic 模型、TypedDict 等。
2. 我们使用 `griffe` 解析 docstring。支持的 docstring 格式为 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测 docstring 格式，但这只是尽力而为；你可以在调用 `function_tool` 时显式设置。你也可以通过将 `use_docstring_info` 设为 `False` 来禁用 docstring 解析。

用于 schema 抽取的代码位于 [`agents.function_schema`][]。

## 将智能体作为工具

在某些工作流中，你可能希望由一个中心智能体编排一组专门化的智能体网络，而不是进行控制权的任务转移。你可以通过将智能体建模为工具来实现。

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

### 工具化智能体的自定义

`agent.as_tool` 函数是一个便捷方法，可轻松将智能体转换为工具。但它并不支持所有配置；例如，你不能设置 `max_turns`。对于高级用例，请在你的工具实现中直接使用 `Runner.run`：

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

在某些情况下，你可能希望在把结果返回给中心智能体之前，修改工具化智能体的输出。以下场景可能会用到：

- 从子智能体的对话历史中提取特定信息（例如一个 JSON 负载）。
- 转换或重格式化智能体的最终答案（例如将 Markdown 转为纯文本或 CSV）。
- 验证输出或在智能体响应缺失或格式错误时提供回退值。

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

### 流式传输的嵌套智能体运行

向 `as_tool` 传入 `on_stream` 回调，以监听嵌套智能体发出的流式事件，同时在流结束后仍返回其最终输出。

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent']['name']} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

预期行为：

- 事件类型与 `StreamEvent["type"]` 相对应：`raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- 提供 `on_stream` 将自动以流式传输模式运行嵌套智能体，并在返回最终输出前耗尽该流。
- 处理器可以是同步或异步的；每个事件会按到达顺序依次传递。
- 当通过模型的工具调用触发时，`tool_call_id` 存在；直接调用时该值可能为 `None`。
- 参见 `examples/agent_patterns/agents_as_tools_streaming.py` 获取完整可运行的 sample code。

### 条件化启用工具

你可以使用 `is_enabled` 参数在运行时有条件地启用或禁用智能体工具。这允许你根据上下文、用户偏好或运行时条件，动态筛选 LLM 可用的工具。

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

`is_enabled` 参数可接受：

- 逻辑值：`True`（总是启用）或 `False`（总是禁用）
- 可调用函数：接受 `(context, agent)` 并返回布尔值的函数
- 异步函数：用于实现复杂条件逻辑的异步函数

被禁用的工具在运行时对 LLM 完全不可见，常用于：

- 基于用户权限的功能门控
- 基于环境的工具可用性（开发 vs 生产）
- 不同工具配置的 A/B 测试
- 基于运行时状态的动态工具过滤

## 实验性：Codex 工具

`codex_tool` 封装了 Codex CLI，使智能体能够在工具调用期间运行以工作区为范围的任务（shell、文件编辑、MCP 工具）。该接口为实验性，可能会变化。

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.2-codex",
                network_access_enabled=True,
                web_search_enabled=False,
            ),
            persist_session=True,
        )
    ],
)
```

须知：

- 认证：设置 `CODEX_API_KEY`（首选）或 `OPENAI_API_KEY`，或传入 `codex_options={"api_key": "..."}`。
- 输入：工具调用必须在 `inputs` 中至少包含一项 `{ "type": "text", "text": ... }` 或 `{ "type": "local_image", "path": ... }`。
- 安全：将 `sandbox_mode` 与 `working_directory` 搭配使用；在非 Git 仓库下设置 `skip_git_repo_check=True`。
- 行为：`persist_session=True` 复用单个 Codex 线程并返回其 `thread_id`。
- 流式传输：`on_stream` 将接收 Codex 事件（推理、命令执行、MCP 工具调用、文件变更、网络检索）。
- 输出：结果包含 `response`、`usage` 和 `thread_id`；`usage` 会添加到 `RunContextWrapper.usage`。
- 结构：当需要类型化输出时，`output_schema` 会强制 Codex 返回结构化响应。
- 参见 `examples/tools/codex.py` 获取完整可运行的 sample code。

## 在函数工具中处理错误

当你通过 `@function_tool` 创建函数工具时，可以传入 `failure_error_function`。这是一个在工具调用崩溃时向 LLM 提供错误响应的函数。

- 默认情况下（即未传入时），会运行 `default_tool_error_function`，告知 LLM 发生了错误。
- 如果你传入了自定义错误函数，将改为运行该函数，并把其响应发送给 LLM。
- 如果你显式传入 `None`，那么任何工具调用错误都会被重新抛出，以便你自行处理。这可能是当模型产生无效 JSON 时的 `ModelBehaviorError`，或你的代码崩溃时的 `UserError`，等等。

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