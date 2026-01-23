---
search:
  exclude: true
---
# 工具

工具让智能体能够采取行动：例如获取数据、运行代码、调用外部 API，甚至进行计算机操作。SDK 支持五类工具：

-   由OpenAI托管的工具：与模型一起在 OpenAI 服务上运行。
-   本地运行时工具：在你的环境中运行（计算机操作、shell、应用补丁）。
-   工具调用：将任何 Python 函数包装为工具。
-   Agents as tools：将智能体暴露为可调用工具，而无需完整的任务转移。
-   实验性：Codex 工具：通过工具调用运行工作区作用域的 Codex 任务。

## 由OpenAI托管的工具

在使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI 提供了一些内置工具：

-   [`WebSearchTool`][agents.tool.WebSearchTool] 让智能体进行网络检索。
-   [`FileSearchTool`][agents.tool.FileSearchTool] 允许从你的 OpenAI 向量存储中检索信息。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 让 LLM 在沙盒环境中执行代码。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程 MCP 服务的工具暴露给模型。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示词生成图像。

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

本地运行时工具在你的环境中执行，并且需要你提供实现：

-   [`ComputerTool`][agents.tool.ComputerTool]：实现 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 接口，以启用 GUI/浏览器自动化。
-   [`ShellTool`][agents.tool.ShellTool] 或 [`LocalShellTool`][agents.tool.LocalShellTool]：提供一个 shell 执行器来运行命令。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：实现 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor]，以在本地应用 diff。

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

## 工具调用

你可以将任何 Python 函数作为工具使用。Agents SDK 会自动设置该工具：

-   工具名称将是 Python 函数的名称（或你也可以提供名称）
-   工具描述将来自该函数的 docstring（或你也可以提供描述）
-   函数输入的 schema 会根据函数参数自动创建
-   每个输入的描述来自该函数的 docstring，除非禁用

我们使用 Python 的 `inspect` 模块提取函数签名，同时使用 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析 docstring，并用 `pydantic` 创建 schema。

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

1.  你可以使用任何 Python 类型作为函数参数，且函数可以是同步或异步的。
2.  如果存在 docstring，则会用于捕获描述和参数描述。
3.  函数可以选择接收 `context`（必须是第一个参数）。你也可以设置覆盖项，例如工具名称、描述、使用哪种 docstring 风格等。
4.  你可以将装饰后的函数传入 tools 列表。

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

除了返回文本输出之外，你还可以返回一个或多个图像或文件作为工具调用的输出。为此，你可以返回以下任意类型：

-   图像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或其 TypedDict 版本 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或其 TypedDict 版本 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   文本：字符串或可转为字符串的对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或其 TypedDict 版本 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义工具调用

有时，你不想将 Python 函数作为工具。你也可以直接创建一个 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

-   `name`
-   `description`
-   `params_json_schema`，即参数的 JSON schema
-   `on_invoke_tool`，即一个异步函数：接收 [`ToolContext`][agents.tool_context.ToolContext] 和以 JSON 字符串形式传入的参数，并且必须将工具输出以字符串形式返回。

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

如前所述，我们会自动解析函数签名以提取工具 schema，并解析 docstring 以提取工具及各参数的描述。补充说明：

1. 签名解析通过 `inspect` 模块完成。我们使用类型注解来理解参数类型，并动态构建一个 Pydantic 模型来表示整体 schema。它支持大多数类型，包括 Python 基础类型、Pydantic 模型、TypedDict 等。
2. 我们使用 `griffe` 解析 docstring。支持的 docstring 格式包括 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测 docstring 格式，但这是尽力而为；你也可以在调用 `function_tool` 时显式设置。你还可以将 `use_docstring_info` 设为 `False` 来禁用 docstring 解析。

schema 提取的代码位于 [`agents.function_schema`][]。

## Agents as tools

在某些工作流中，你可能希望由一个中心智能体来编排一组专门的智能体，而不是进行控制权任务转移。你可以通过将智能体建模为工具来实现这一点。

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

`agent.as_tool` 函数是一个便捷方法，用于更轻松地将智能体转换为工具。但它不支持所有配置；例如，你无法设置 `max_turns`。对于高级用例，请在你的工具实现中直接使用 `Runner.run`：

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

在某些情况下，你可能希望在将工具智能体的输出返回给中心智能体之前对其进行修改。这在以下场景中可能很有用：

-   从子智能体的聊天历史中提取特定信息（例如 JSON payload）。
-   转换或重新格式化智能体的最终答案（例如将 Markdown 转为纯文本或 CSV）。
-   校验输出，或在智能体响应缺失或格式不正确时提供回退值。

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

### 流式传输嵌套智能体运行

向 `as_tool` 传入 `on_stream` 回调，以监听嵌套智能体发出的流式事件，同时在流完成后仍返回其最终输出。

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

- 事件类型与 `StreamEvent["type"]` 镜像一致：`raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- 提供 `on_stream` 会自动以流式模式运行嵌套智能体，并在返回最终输出之前消费完流。
- 处理器可以是同步或异步的；每个事件会按到达顺序依次交付。
- 当工具通过模型的工具调用触发时会包含 `tool_call_id`；直接调用时可能为 `None`。
- 完整可运行示例参见 `examples/agent_patterns/agents_as_tools_streaming.py`。

### 条件式工具启用

你可以使用 `is_enabled` 参数在运行时按条件启用或禁用智能体工具。这使你能够基于上下文、用户偏好或运行时条件，动态过滤 LLM 可用的工具。

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

`is_enabled` 参数支持：

-   **布尔值**：`True`（始终启用）或 `False`（始终禁用）
-   **可调用函数**：接收 `(context, agent)` 并返回布尔值的函数
-   **异步函数**：用于复杂条件逻辑的异步函数

被禁用的工具在运行时对 LLM 完全隐藏，因此可用于：

-   基于用户权限的功能门控
-   环境特定的工具可用性（dev vs prod）
-   对不同工具配置进行 A/B 测试
-   基于运行时状态的动态工具过滤

## 实验性：Codex 工具

`codex_tool` 封装了 Codex CLI，使智能体能够在一次工具调用期间运行工作区作用域的任务（shell、文件编辑、MCP 工具）。该接口为实验性，可能会发生变化。

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

注意事项：

-   认证：设置 `CODEX_API_KEY`（推荐）或 `OPENAI_API_KEY`，或传入 `codex_options={"api_key": "..."}`。
-   运行时：`codex_options.base_url` 会覆盖 CLI base URL，`codex_options.codex_path_override`（或 `CODEX_PATH`）用于选择二进制文件。
-   环境：`codex_options.env` 完全控制子进程环境。提供后，子进程不会继承 `os.environ`。
-   输入：工具调用必须在 `inputs` 中至少包含一项 `{ "type": "text", "text": ... }` 或 `{ "type": "local_image", "path": ... }`。
-   安全：将 `sandbox_mode` 与 `working_directory` 配对使用；在非 Git 仓库中设置 `skip_git_repo_check=True`。
-   行为：`persist_session=True` 会复用单个 Codex thread，并返回其 `thread_id`。
-   流式传输：`on_stream` 会接收 Codex 事件（推理、命令执行、MCP 工具调用、文件变更、网络检索）。
-   输出：结果包含 `response`、`usage` 和 `thread_id`；usage 会加入到 `RunContextWrapper.usage`。
-   结构：当你需要类型化输出时，`output_schema` 会强制 Codex 以 structured outputs 响应。
-   完整可运行示例参见 `examples/tools/codex.py`。

## 在工具调用中处理错误

当你通过 `@function_tool` 创建工具调用时，可以传入 `failure_error_function`。这是一个在工具调用崩溃时向 LLM 提供错误响应的函数。

-   默认情况下（即不传任何值），会运行 `default_tool_error_function`，告知 LLM 发生了错误。
-   如果你传入自定义错误函数，则会改为运行该函数，并将其响应发送给 LLM。
-   如果你显式传入 `None`，则任何工具调用错误都会被重新抛出，由你处理。例如，如果模型生成了无效 JSON，则可能是 `ModelBehaviorError`；如果你的代码崩溃，则可能是 `UserError` 等。

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

如果你是手动创建 `FunctionTool` 对象，那么你必须在 `on_invoke_tool` 函数内部处理错误。