---
search:
  exclude: true
---
# 工具

工具让智能体能够执行操作：例如获取数据、运行代码、调用外部 API，甚至操作计算机。SDK 支持五类：

-   由OpenAI托管的工具：与模型一起在 OpenAI 服务上运行。
-   本地/运行时执行工具：`ComputerTool` 和 `ApplyPatchTool` 始终在你的环境中运行，而 `ShellTool` 可在本地或托管容器中运行。
-   Function Calling：将任意 Python 函数封装为工具。
-   Agents as tools：将智能体作为可调用工具暴露，而无需完整任务转移。
-   实验性：Codex 工具：通过工具调用运行工作区范围内的 Codex 任务。

## 工具类型选择

将本页作为目录使用，然后跳转到与你可控运行时匹配的章节。

| 如果你想... | 从这里开始 |
| --- | --- |
| 使用由 OpenAI 管理的工具（网络检索、文件检索、Code Interpreter、托管 MCP、图像生成） | [托管工具](#hosted-tools) |
| 通过工具搜索将大型工具集合延迟到运行时加载 | [托管工具搜索](#hosted-tool-search) |
| 在你自己的进程或环境中运行工具 | [本地运行时工具](#local-runtime-tools) |
| 将 Python 函数封装为工具 | [工具调用](#function-tools) |
| 让一个智能体在不任务转移的情况下调用另一个智能体 | [Agents as tools](#agents-as-tools) |
| 从智能体运行工作区范围内的 Codex 任务 | [实验性：Codex 工具](#experimental-codex-tool) |

## 托管工具

在使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI 提供了一些内置工具：

-   [`WebSearchTool`][agents.tool.WebSearchTool] 让智能体可以搜索网络。
-   [`FileSearchTool`][agents.tool.FileSearchTool] 允许从你的 OpenAI 向量存储中检索信息。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 让 LLM 在沙箱环境中执行代码。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程 MCP 服务的工具暴露给模型。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示词生成图像。
-   [`ToolSearchTool`][agents.tool.ToolSearchTool] 让模型按需加载延迟工具、命名空间或托管 MCP 服务。

高级托管搜索选项：

-   `FileSearchTool` 除了 `vector_store_ids` 和 `max_num_results` 外，还支持 `filters`、`ranking_options` 和 `include_search_results`。
-   `WebSearchTool` 支持 `filters`、`user_location` 和 `search_context_size`。

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

### 托管工具搜索

工具搜索让 OpenAI Responses 模型将大型工具集合延迟到运行时，因此模型只会加载当前轮次所需的子集。当你拥有大量工具调用、命名空间分组或托管 MCP 服务，并希望减少工具 schema token 而不在前期暴露所有工具时，这非常有用。

当候选工具在构建智能体时已知时，优先使用托管工具搜索。如果你的应用需要动态决定加载内容，Responses API 也支持客户端执行的工具搜索，但标准 `Runner` 不会自动执行该模式。

```python
from typing import Annotated

from agents import Agent, Runner, ToolSearchTool, function_tool, tool_namespace


@function_tool(defer_loading=True)
def get_customer_profile(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """Fetch a CRM customer profile."""
    return f"profile for {customer_id}"


@function_tool(defer_loading=True)
def list_open_orders(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """List open orders for a customer."""
    return f"open orders for {customer_id}"


crm_tools = tool_namespace(
    name="crm",
    description="CRM tools for customer lookups.",
    tools=[get_customer_profile, list_open_orders],
)


agent = Agent(
    name="Operations assistant",
    model="gpt-5.4",
    instructions="Load the crm namespace before using CRM tools.",
    tools=[*crm_tools, ToolSearchTool()],
)

result = await Runner.run(agent, "Look up customer_42 and list their open orders.")
print(result.final_output)
```

注意事项：

-   托管工具搜索仅适用于 OpenAI Responses 模型。当前 Python SDK 支持依赖 `openai>=2.25.0`。
-   当你在智能体上配置延迟加载集合时，精确添加一个 `ToolSearchTool()`。
-   可搜索集合包括 `@function_tool(defer_loading=True)`、`tool_namespace(name=..., description=..., tools=[...])` 和 `HostedMCPTool(tool_config={..., "defer_loading": True})`。
-   延迟加载的工具调用必须与 `ToolSearchTool()` 搭配使用。仅命名空间配置也可使用 `ToolSearchTool()` 以便模型按需加载正确分组。
-   `tool_namespace()` 在共享命名空间名称和描述下对 `FunctionTool` 实例分组。当你有许多相关工具（如 `crm`、`billing` 或 `shipping`）时，这通常是最佳选择。
-   OpenAI 官方最佳实践指南是 [Use namespaces where possible](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible)。
-   在可能的情况下，优先使用命名空间或托管 MCP 服务，而不是大量单独延迟函数。它们通常能为模型提供更好的高层搜索面，并带来更好的 token 节省。
-   命名空间可以混合即时工具和延迟工具。未设置 `defer_loading=True` 的工具仍可立即调用，而同一命名空间中的延迟工具通过工具搜索加载。
-   经验法则是让每个命名空间保持较小规模，理想情况下少于 10 个函数。
-   命名 `tool_choice` 不能定位到裸命名空间名或仅延迟工具。优先使用 `auto`、`required` 或真实的顶层可调用工具名。
-   `ToolSearchTool(execution="client")` 用于手动 Responses 编排。如果模型输出客户端执行的 `tool_search_call`，标准 `Runner` 会抛出异常而不是替你执行。
-   工具搜索活动会出现在 [`RunResult.new_items`](results.md#new-items) 以及 [`RunItemStreamEvent`](streaming.md#run-item-event-names) 中，并使用专用条目和事件类型。
-   参见 `examples/tools/tool_search.py`，其中有涵盖命名空间加载和顶层延迟工具的完整可运行代码示例。
-   官方平台指南：[Tool search](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### 托管容器 Shell + 技能

`ShellTool` 也支持 OpenAI 托管容器执行。当你希望模型在托管容器而不是本地运行时执行 shell 命令时，请使用此模式。

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.4",
    instructions="Use the mounted skill when helpful.",
    tools=[
        ShellTool(
            environment={
                "type": "container_auto",
                "network_policy": {"type": "disabled"},
                "skills": [csv_skill],
            }
        )
    ],
)

result = await Runner.run(
    agent,
    "Use the configured skill to analyze CSV files in /mnt/data and summarize totals by region.",
)
print(result.final_output)
```

如需在后续运行中复用现有容器，设置 `environment={"type": "container_reference", "container_id": "cntr_..."}`。

注意事项：

-   托管 shell 可通过 Responses API shell 工具使用。
-   `container_auto` 为请求配置容器；`container_reference` 复用现有容器。
-   `container_auto` 还可包含 `file_ids` 和 `memory_limit`。
-   `environment.skills` 接受技能引用和内联技能包。
-   在托管环境下，不要在 `ShellTool` 上设置 `executor`、`needs_approval` 或 `on_approval`。
-   `network_policy` 支持 `disabled` 和 `allowlist` 模式。
-   在 allowlist 模式下，`network_policy.domain_secrets` 可按名称注入域级密钥。
-   参见 `examples/tools/container_shell_skill_reference.py` 和 `examples/tools/container_shell_inline_skill.py` 获取完整代码示例。
-   OpenAI 平台指南：[Shell](https://platform.openai.com/docs/guides/tools-shell) 和 [Skills](https://platform.openai.com/docs/guides/tools-skills)。

## 本地运行时工具

本地运行时工具在模型响应本身之外执行。模型仍决定何时调用它们，但实际工作由你的应用或配置的执行环境完成。

`ComputerTool` 和 `ApplyPatchTool` 始终需要你提供本地实现。`ShellTool` 同时覆盖两种模式：当你希望托管执行时，使用上方托管容器配置；当你希望命令在自己的进程中运行时，使用下方本地运行时配置。

本地运行时工具需要你提供实现：

-   [`ComputerTool`][agents.tool.ComputerTool]：实现 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 接口以启用 GUI/浏览器自动化。
-   [`ShellTool`][agents.tool.ShellTool]：同时支持本地执行和托管容器执行的最新 shell 工具。
-   [`LocalShellTool`][agents.tool.LocalShellTool]：旧版本地 shell 集成。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：实现 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] 以在本地应用 diff。
-   本地 shell 技能可通过 `ShellTool(environment={"type": "local", "skills": [...]})` 使用。

### ComputerTool 与 Responses 计算机工具

`ComputerTool` 仍是本地 harness：你提供 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 实现，SDK 将该 harness 映射到 OpenAI Responses API 的计算机能力面。

对于显式的 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 请求，SDK 发送 GA 内置工具负载 `{"type": "computer"}`。较旧的 `computer-use-preview` 模型继续使用预览负载 `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}`。这与 OpenAI [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use/) 中描述的平台迁移一致：

-   模型：`computer-use-preview` -> `gpt-5.4`
-   工具选择器：`computer_use_preview` -> `computer`
-   计算机调用形态：每个 `computer_call` 一个 `action` -> `computer_call` 上批量 `actions[]`
-   截断：预览路径需要 `ModelSettings(truncation="auto")` -> GA 路径不需要

SDK 根据实际 Responses 请求中的生效模型选择该线协议形态。如果你使用 prompt 模板且请求因 prompt 持有模型而省略 `model`，SDK 会保持预览兼容的计算机负载，除非你显式保留 `model="gpt-5.4"`，或通过 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制使用 GA 选择器。

当存在 [`ComputerTool`][agents.tool.ComputerTool] 时，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 都会被接受，并标准化为与生效请求模型匹配的内置选择器。没有 `ComputerTool` 时，这些字符串仍表现为普通函数名。

当 `ComputerTool` 由 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂支持时，这一区别尤为重要。GA `computer` 负载在序列化时不需要 `environment` 或尺寸，因此未解析工厂也没问题。预览兼容序列化仍需要已解析的 `Computer` 或 `AsyncComputer` 实例，以便 SDK 发送 `environment`、`display_width` 和 `display_height`。

在运行时，两条路径仍使用同一本地 harness。预览响应会输出带单个 `action` 的 `computer_call` 条目；`gpt-5.4` 可输出批量 `actions[]`，SDK 会按顺序执行，然后产出 `computer_call_output` 截图条目。参见 `examples/tools/computer_use.py` 获取基于 Playwright 的可运行 harness。

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

你可以将任何 Python 函数用作工具。Agents SDK 会自动完成工具设置：

-   工具名称将是 Python 函数名（也可自行提供名称）
-   工具描述将取自函数 docstring（也可自行提供描述）
-   函数输入 schema 会根据函数参数自动创建
-   每个输入的描述将取自函数 docstring，除非禁用

我们使用 Python 的 `inspect` 模块提取函数签名，配合 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析 docstring，并使用 `pydantic` 创建 schema。

当你使用 OpenAI Responses 模型时，`@function_tool(defer_loading=True)` 会隐藏工具调用，直到由 `ToolSearchTool()` 加载。你也可以使用 [`tool_namespace()`][agents.tool.tool_namespace] 对相关工具调用分组。完整设置和约束请参见 [托管工具搜索](#hosted-tool-search)。

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

1.  你可以在函数参数中使用任意 Python 类型，且函数可为同步或异步。
2.  如有 docstring，会用于提取描述和参数描述。
3.  函数可选择接收 `context`（必须是第一个参数）。你也可以设置覆盖项，例如工具名、描述、使用哪种 docstring 风格等。
4.  你可以将装饰后的函数传入工具列表。

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

### 工具调用返回图像或文件

除了返回文本输出外，你还可以将一个或多个图像或文件作为工具调用的输出返回。可返回以下任意类型：

-   图像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或其 TypedDict 版本 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或其 TypedDict 版本 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   文本：字符串或可转字符串对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或其 TypedDict 版本 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义工具调用

有时你不想将 Python 函数作为工具。你也可以直接创建 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

-   `name`
-   `description`
-   `params_json_schema`，即参数的 JSON schema
-   `on_invoke_tool`，一个异步函数，接收 [`ToolContext`][agents.tool_context.ToolContext] 和 JSON 字符串形式的参数，并返回工具输出（例如文本、结构化工具输出对象或输出列表）。

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

### 参数与 docstring 自动解析

如前所述，我们会自动解析函数签名以提取工具 schema，并解析 docstring 以提取工具及各参数描述。说明如下：

1. 签名解析通过 `inspect` 模块完成。我们使用类型注解理解参数类型，并动态构建 Pydantic 模型表示整体 schema。它支持大多数类型，包括 Python 基本类型、Pydantic 模型、TypedDict 等。
2. 我们使用 `griffe` 解析 docstring。支持的 docstring 格式包括 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测 docstring 格式，但这属于尽力而为；你也可在调用 `function_tool` 时显式设置。你还可以通过将 `use_docstring_info` 设为 `False` 来禁用 docstring 解析。

schema 提取代码位于 [`agents.function_schema`][]。

### 使用 Pydantic Field 约束和描述参数

你可以使用 Pydantic 的 [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) 为工具参数添加约束（例如数字最小/最大值、字符串长度或模式）和描述。与 Pydantic 一致，两种形式都支持：基于默认值（`arg: int = Field(..., ge=1)`）和 `Annotated`（`arg: Annotated[int, Field(..., ge=1)]`）。生成的 JSON schema 和校验都会包含这些约束。

```python
from typing import Annotated
from pydantic import Field
from agents import function_tool

# Default-based form
@function_tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"

# Annotated form
@function_tool
def score_b(score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")]) -> str:
    return f"Score recorded: {score}"
```

### 工具调用超时

你可以通过 `@function_tool(timeout=...)` 为异步工具调用设置每次调用超时。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

当达到超时时，默认行为是 `timeout_behavior="error_as_result"`，即向模型发送可见的超时消息（例如 `Tool 'slow_lookup' timed out after 2 seconds.`）。

你可以控制超时处理方式：

-   `timeout_behavior="error_as_result"`（默认）：向模型返回超时消息，使其可恢复。
-   `timeout_behavior="raise_exception"`：抛出 [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] 并使运行失败。
-   `timeout_error_function=...`：在使用 `error_as_result` 时自定义超时消息。

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError, function_tool


@function_tool(timeout=1.5, timeout_behavior="raise_exception")
async def slow_tool() -> str:
    await asyncio.sleep(5)
    return "done"


agent = Agent(name="Timeout hard-fail", tools=[slow_tool])

try:
    await Runner.run(agent, "Run the tool")
except ToolTimeoutError as e:
    print(f"{e.tool_name} timed out in {e.timeout_seconds} seconds")
```

!!! note

    超时配置仅支持异步 `@function_tool` 处理器。

### 处理工具调用中的错误

当你通过 `@function_tool` 创建工具调用时，可以传入 `failure_error_function`。这是在工具调用崩溃时向 LLM 提供错误响应的函数。

-   默认情况下（即你未传任何值），会运行 `default_tool_error_function`，告知 LLM 发生了错误。
-   如果你传入自己的错误函数，则运行该函数，并将其响应发送给 LLM。
-   如果你显式传入 `None`，则任何工具调用错误都会被重新抛出供你处理。这可能是模型生成了无效 JSON 导致的 `ModelBehaviorError`，也可能是你的代码崩溃导致的 `UserError` 等。

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

如果你是手动创建 `FunctionTool` 对象，则必须在 `on_invoke_tool` 函数中处理错误。

## Agents as tools

在某些工作流中，你可能希望由一个中心智能体编排一组专用智能体，而不是移交控制权。你可以通过将智能体建模为工具来实现。

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

### 工具智能体自定义

`agent.as_tool` 函数是一个便捷方法，便于将智能体转换为工具。它支持常见运行时选项，例如 `max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session` 和 `needs_approval`。它还通过 `parameters`、`input_builder` 和 `include_input_schema` 支持结构化输入。对于高级编排（例如条件重试、回退行为或链式多个智能体调用），请在你的工具实现中直接使用 `Runner.run`：

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

### 工具智能体的结构化输入

默认情况下，`Agent.as_tool()` 期望单个字符串输入（`{"input": "..."}`），但你可以通过传入 `parameters`（Pydantic 模型或 dataclass 类型）暴露结构化 schema。

附加选项：

- `include_input_schema=True` 会在生成的嵌套输入中包含完整 JSON Schema。
- `input_builder=...` 允许你完全自定义结构化工具参数如何转换为嵌套智能体输入。
- `RunContextWrapper.tool_input` 在嵌套运行上下文中包含已解析的结构化负载。

```python
from pydantic import BaseModel, Field


class TranslationInput(BaseModel):
    text: str = Field(description="Text to translate.")
    source: str = Field(description="Source language.")
    target: str = Field(description="Target language.")


translator_tool = translator_agent.as_tool(
    tool_name="translate_text",
    tool_description="Translate text between languages.",
    parameters=TranslationInput,
    include_input_schema=True,
)
```

参见 `examples/agent_patterns/agents_as_tools_structured.py` 获取完整可运行代码示例。

### 工具智能体的审批门控

`Agent.as_tool(..., needs_approval=...)` 使用与 `function_tool` 相同的审批流程。如果需要审批，运行会暂停，待处理条目会出现在 `result.interruptions`；随后使用 `result.to_state()`，并在调用 `state.approve(...)` 或 `state.reject(...)` 后继续。完整暂停/恢复模式请参见 [Human-in-the-loop guide](human_in_the_loop.md)。

### 自定义输出提取

在某些情况下，你可能希望在将工具智能体输出返回给中心智能体之前进行修改。这在以下场景可能有用：

-   从子智能体聊天历史中提取特定信息（例如 JSON 负载）。
-   转换或重格式化智能体最终答案（例如将 Markdown 转为纯文本或 CSV）。
-   当智能体响应缺失或格式错误时，验证输出或提供回退值。

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

在自定义提取器内部，嵌套的 [`RunResult`][agents.result.RunResult] 还会暴露
[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]，这在
你需要外层工具名、调用 ID 或原始参数来进行嵌套结果后处理时非常有用。
参见 [Results guide](results.md#agent-as-tool-metadata)。

### 流式传输嵌套智能体运行

向 `as_tool` 传入 `on_stream` 回调，以监听嵌套智能体发出的流式事件，同时在流完成后仍返回其最终输出。

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent'].name} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

预期行为：

- 事件类型与 `StreamEvent["type"]` 一致：`raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- 提供 `on_stream` 会自动让嵌套智能体以流式模式运行，并在返回最终输出前消费完整流。
- 处理器可以是同步或异步；每个事件按到达顺序交付。
- 通过模型工具调用触发时会有 `tool_call`；直接调用时它可能为 `None`。
- 完整可运行示例参见 `examples/agent_patterns/agents_as_tools_streaming.py`。

### 条件性启用工具

你可以使用 `is_enabled` 参数在运行时条件性启用或禁用智能体工具。这使你能够根据上下文、用户偏好或运行时条件动态筛选哪些工具对 LLM 可用。

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

-   **布尔值**：`True`（始终启用）或 `False`（始终禁用）
-   **可调用函数**：接收 `(context, agent)` 并返回布尔值的函数
-   **异步函数**：用于复杂条件逻辑的异步函数

被禁用的工具在运行时会对 LLM 完全隐藏，这在以下场景很有用：

-   基于用户权限的功能门控
-   特定环境下的工具可用性（开发 vs 生产）
-   不同工具配置的 A/B 测试
-   基于运行时状态的动态工具筛选

## 实验性：Codex 工具

`codex_tool` 封装了 Codex CLI，使智能体能够在工具调用期间运行工作区范围任务（shell、文件编辑、MCP 工具）。该能力面为实验性，可能变更。

当你希望主智能体在不离开当前运行的前提下，将受限工作区任务委派给 Codex 时可使用它。默认工具名为 `codex`。若设置自定义名称，必须为 `codex` 或以 `codex_` 开头。当智能体包含多个 Codex 工具时，每个名称必须唯一。

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, TurnOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.4",
                model_reasoning_effort="low",
                network_access_enabled=True,
                web_search_mode="disabled",
                approval_policy="never",
            ),
            default_turn_options=TurnOptions(
                idle_timeout_seconds=60,
            ),
            persist_session=True,
        )
    ],
)
```

从这些选项组开始：

-   执行能力面：`sandbox_mode` 和 `working_directory` 定义 Codex 可操作范围。请配对使用；当工作目录不在 Git 仓库内时，设置 `skip_git_repo_check=True`。
-   线程默认值：`default_thread_options=ThreadOptions(...)` 配置模型、推理力度、审批策略、附加目录、网络访问和网络检索模式。优先使用 `web_search_mode`，而不是旧版 `web_search_enabled`。
-   轮次默认值：`default_turn_options=TurnOptions(...)` 配置每轮行为，如 `idle_timeout_seconds` 和可选取消 `signal`。
-   工具 I/O：工具调用必须至少包含一个 `inputs` 条目，格式为 `{ "type": "text", "text": ... }` 或 `{ "type": "local_image", "path": ... }`。`output_schema` 可用于要求结构化 Codex 响应。

线程复用与持久化是分离控制项：

-   `persist_session=True` 会在对同一工具实例重复调用时复用一个 Codex 线程。
-   `use_run_context_thread_id=True` 会在共享同一可变上下文对象的跨运行中，在运行上下文中存储并复用线程 ID。
-   线程 ID 优先级为：每次调用的 `thread_id`，然后运行上下文线程 ID（若启用），再然后是已配置的 `thread_id` 选项。
-   默认运行上下文键为：当 `name="codex"` 时为 `codex_thread_id`，当 `name="codex_<suffix>"` 时为 `codex_thread_id_<suffix>`。可用 `run_context_thread_id_key` 覆盖。

运行时配置：

-   鉴权：设置 `CODEX_API_KEY`（推荐）或 `OPENAI_API_KEY`，或传入 `codex_options={"api_key": "..."}`。
-   运行时：`codex_options.base_url` 覆盖 CLI base URL。
-   二进制解析：设置 `codex_options.codex_path_override`（或 `CODEX_PATH`）以固定 CLI 路径。否则 SDK 会先从 `PATH` 解析 `codex`，再回退到内置 vendor 二进制。
-   环境：`codex_options.env` 完整控制子进程环境。提供后，子进程不会继承 `os.environ`。
-   流限制：`codex_options.codex_subprocess_stream_limit_bytes`（或 `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`）控制 stdout/stderr 读取器限制。有效范围为 `65536` 到 `67108864`；默认值为 `8388608`。
-   流式传输：`on_stream` 接收线程/轮次生命周期事件和条目事件（`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list` 和 `error` 条目更新）。
-   输出：结果包含 `response`、`usage` 和 `thread_id`；usage 会添加到 `RunContextWrapper.usage`。

参考：

-   [Codex 工具 API 参考](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions 参考](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions 参考](ref/extensions/experimental/codex/turn_options.md)
-   完整可运行代码示例参见 `examples/tools/codex.py` 和 `examples/tools/codex_same_thread.py`。