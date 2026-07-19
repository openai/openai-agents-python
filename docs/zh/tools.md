---
search:
  exclude: true
---
# 工具

工具让智能体能够执行操作，例如获取数据、运行代码、调用外部 API，甚至操作计算机。SDK 支持五个目录：

-   由OpenAI托管的工具：与模型一起在OpenAI服务上运行。
-   本地/运行时执行工具：`ComputerTool` 和 `ApplyPatchTool` 始终在你的环境中运行，而 `ShellTool` 可以在本地或托管容器中运行。
-   Function calling：将任意 Python 函数封装为工具。
-   Agents as tools：将智能体公开为可调用工具，而无需执行完整的任务转移。
-   实验性功能：Codex 工具：通过工具调用运行限定于工作区的 Codex 任务。

## 工具类型选择

将本页面作为目录使用，然后跳转到与你所控制的运行时相匹配的部分。

| 如果你想要…… | 从这里开始 |
| --- | --- |
| 使用由OpenAI管理的工具（网络检索、文件检索、Code Interpreter、托管式MCP、图像生成） | [托管工具](#hosted-tools) |
| 使用工具搜索将大型工具集合延迟到运行时加载 | [托管工具搜索](#hosted-tool-search) |
| 在你自己的进程或环境中运行工具 | [本地运行时工具](#local-runtime-tools) |
| 将 Python 函数封装为工具 | [工具调用](#function-tools) |
| 让一个智能体调用另一个智能体而不执行任务转移 | [Agents as tools](#agents-as-tools) |
| 从智能体运行限定于工作区的 Codex 任务 | [实验性功能：Codex 工具](#experimental-codex-tool) |

## 托管工具

使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI提供了一些内置工具：

-   [`WebSearchTool`][agents.tool.WebSearchTool] 让智能体能够检索网络。
-   [`FileSearchTool`][agents.tool.FileSearchTool] 支持从你的OpenAI向量存储中检索信息。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 让LLM能够在沙箱环境中执行代码。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程MCP服务的工具公开给模型。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示词生成图像。
-   [`ToolSearchTool`][agents.tool.ToolSearchTool] 让模型能够按需加载延迟加载的工具、命名空间或托管式MCP服务。

高级托管搜索选项：

-   除了 `vector_store_ids` 和 `max_num_results`，`FileSearchTool` 还支持 `filters`、`ranking_options` 和 `include_search_results`。
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

工具搜索让 OpenAI Responses 模型可以将大型工具集合延迟到运行时加载，使模型仅加载当前轮次所需的子集。当你拥有大量工具调用、命名空间组或托管式MCP服务，并且希望在不预先公开每个工具的情况下减少工具模式所占的 token 时，此功能非常有用。

如果构建智能体时已经知道候选工具，请从托管工具搜索开始。如果你的应用需要动态决定加载哪些内容，Responses API 也支持由客户端执行的工具搜索，但标准 `Runner` 不会自动执行该模式。

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
    model="gpt-5.6-sol",
    instructions="Load the crm namespace before using CRM tools.",
    tools=[*crm_tools, ToolSearchTool()],
)

result = await Runner.run(agent, "Look up customer_42 and list their open orders.")
print(result.final_output)
```

注意事项：

-   托管工具搜索仅适用于 OpenAI Responses 模型。当前 Python SDK 的支持依赖于 `openai>=2.25.0`。
-   在智能体上配置延迟加载的工具集合时，只添加一个 `ToolSearchTool()`。
-   可搜索的工具集合包括 `@function_tool(defer_loading=True)`、`tool_namespace(name=..., description=..., tools=[...])` 和 `HostedMCPTool(tool_config={..., "defer_loading": True})`。
-   延迟加载的工具调用必须与 `ToolSearchTool()` 配合使用。仅包含命名空间的设置也可以使用 `ToolSearchTool()`，让模型按需加载正确的工具组。
-   `tool_namespace()` 将多个 `FunctionTool` 实例归入一个具有共享名称和描述的命名空间。当你有许多相关工具（例如 `crm`、`billing` 或 `shipping`）时，这通常是最佳选择。
-   OpenAI的官方最佳实践指南是[尽可能使用命名空间](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible)。
-   如果可能，优先使用命名空间或托管式MCP服务，而不是许多单独延迟加载的函数。它们通常能为模型提供更好的高层搜索界面，并节省更多 token。
-   命名空间可以混合包含立即可用和延迟加载的工具。未设置 `defer_loading=True` 的工具仍可立即调用，而同一命名空间中的延迟工具会通过工具搜索加载。
-   根据经验，每个命名空间应保持相对精简，最好少于 10 个函数。
-   具名 `tool_choice` 无法指定单独的命名空间名称或仅支持延迟加载的工具。应优先使用 `auto`、`required` 或实际的顶层可调用工具名称。
-   `ToolSearchTool(execution="client")` 用于手动编排 Responses。如果模型发出由客户端执行的 `tool_search_call`，标准 `Runner` 会引发异常，而不会替你执行。
-   工具搜索活动会显示在 [`RunResult.new_items`](results.md#new-items) 和 [`RunItemStreamEvent`](streaming.md#run-item-event-names) 中，并使用专门的条目类型和事件类型。
-   有关涵盖命名空间加载和顶层延迟工具的完整可运行代码示例，请参阅 `examples/tools/tool_search.py`。
-   官方平台指南：[工具搜索](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### 托管容器 Shell 与技能

`ShellTool` 还支持在OpenAI托管的容器中执行。当你希望模型在托管容器中而不是本地运行时中执行 Shell 命令时，请使用此模式。

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.6-sol",
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

若要在后续运行中复用现有容器，请设置 `environment={"type": "container_reference", "container_id": "cntr_..."}`。

注意事项：

-   托管 Shell 通过 Responses API 的 Shell 工具提供。
-   `container_auto` 为请求创建容器；`container_reference` 复用现有容器。
-   `container_auto` 还可以包含 `file_ids` 和 `memory_limit`。
-   `environment.skills` 接受技能引用和内联技能包。
-   使用托管环境时，不要在 `ShellTool` 上设置 `executor`、`needs_approval` 或 `on_approval`。
-   `network_policy` 支持 `disabled` 和 `allowlist` 模式。
-   在允许列表模式下，`network_policy.domain_secrets` 可以按名称注入限定于特定域名的密钥。
-   有关完整代码示例，请参阅 `examples/tools/container_shell_skill_reference.py` 和 `examples/tools/container_shell_inline_skill.py`。
-   OpenAI平台指南：[Shell](https://platform.openai.com/docs/guides/tools-shell)和[技能](https://platform.openai.com/docs/guides/tools-skills)。

## 本地运行时工具

本地运行时工具在模型响应本身之外执行。模型仍会决定何时调用它们，但实际工作由你的应用或配置的执行环境完成。

`ComputerTool` 和 `ApplyPatchTool` 始终需要由你提供本地实现。`ShellTool` 横跨两种模式：如果需要托管执行，请使用上面的托管容器配置；如果希望命令在你自己的进程中运行，请使用下面的本地运行时配置。

本地运行时工具要求你提供实现：

-   [`ComputerTool`][agents.tool.ComputerTool]：实现 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 接口，以启用 GUI/浏览器自动化。
-   [`ShellTool`][agents.tool.ShellTool]：同时用于本地执行和托管容器执行的最新 Shell 工具。
-   [`LocalShellTool`][agents.tool.LocalShellTool]：旧版本地 Shell 集成。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：实现 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor]，以便在本地应用差异。
-   使用 `ShellTool(environment={"type": "local", "skills": [...]})` 可以提供本地 Shell 技能。

### ComputerTool 与 Responses 计算机操作工具

`ComputerTool` 仍然是本地执行框架：你需要提供 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 实现，SDK 会将该执行框架映射到 OpenAI Responses API 的计算机操作界面。

对于显式的 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) 请求，SDK 会发送正式版内置工具载荷 `{"type": "computer"}`。较旧的 `computer-use-preview` 模型会继续使用预览版载荷 `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}`。这与OpenAI[计算机操作指南](https://developers.openai.com/api/docs/guides/tools-computer-use/)中描述的平台迁移一致：

-   模型：`computer-use-preview` -> `gpt-5.5`
-   工具选择器：`computer_use_preview` -> `computer`
-   计算机调用结构：每个 `computer_call` 一个 `action` -> `computer_call` 上批量的 `actions[]`
-   截断：预览版路径要求设置 `ModelSettings(truncation="auto")` -> 正式版路径不要求

SDK 会根据实际 Responses 请求中的有效模型选择相应的传输格式。如果你使用提示词模板，并且由于模型由提示词指定而使请求省略了 `model`，SDK 会保留兼容预览版的计算机操作载荷，除非你明确保留 `model="gpt-5.5"`，或通过 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制使用正式版选择器。

存在 [`ComputerTool`][agents.tool.ComputerTool] 时，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 都会被接受，并规范化为与有效请求模型匹配的内置选择器。如果不存在 `ComputerTool`，这些字符串仍会像普通函数名称一样处理。

当 `ComputerTool` 由 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂支持时，这一区别十分重要。正式版 `computer` 载荷在序列化时不需要 `environment` 或尺寸，因此工厂尚未解析也没有问题。兼容预览版的序列化仍需要已解析的 `Computer` 或 `AsyncComputer` 实例，以便 SDK 可以发送 `environment`、`display_width` 和 `display_height`。

在运行时，两条路径仍使用相同的本地执行框架。预览版响应会发出包含单个 `action` 的 `computer_call` 条目；`gpt-5.5` 可以发出批量的 `actions[]`，SDK 会按顺序执行这些操作，然后生成 `computer_call_output` 截图条目。有关基于 Playwright 的可运行执行框架，请参阅 `examples/tools/computer_use.py`。

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

你可以将任意 Python 函数用作工具。Agents SDK 会自动设置该工具：

-   工具名称将是 Python 函数的名称（你也可以自行提供名称）
-   工具描述将取自函数的文档字符串（你也可以自行提供描述）
-   函数输入的模式会根据函数参数自动创建
-   除非禁用，否则每个输入的描述都取自函数的文档字符串

我们使用 Python 的 `inspect` 模块提取函数签名，同时使用 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析文档字符串，并使用 `pydantic` 创建模式。

使用 OpenAI Responses 模型时，`@function_tool(defer_loading=True)` 会隐藏工具调用，直到 `ToolSearchTool()` 将其加载。你还可以使用 [`tool_namespace()`][agents.tool.tool_namespace] 对相关工具调用进行分组。有关完整设置和限制，请参阅[托管工具搜索](#hosted-tool-search)。

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

1.  你可以使用任意 Python 类型作为函数参数，并且函数可以是同步或异步函数。
2.  如果存在文档字符串，则会使用它来获取函数描述和参数描述。
3.  函数可以选择接收 `context`（必须是第一个参数）。你还可以设置覆盖项，例如工具名称、描述、要使用的文档字符串样式等。
4.  你可以将装饰后的函数传入工具列表。

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

除了返回文本输出，你还可以将一个或多个图像或文件作为工具调用的输出返回。为此，你可以返回以下任意内容：

-   图像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或 TypedDict 版本 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或 TypedDict 版本 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   文本：字符串、可转换为字符串的对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或 TypedDict 版本 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义工具调用

有时，你可能不想将 Python 函数用作工具。如果愿意，你可以直接创建 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

-   `name`
-   `description`
-   `params_json_schema`，即参数的 JSON 模式
-   `on_invoke_tool`，这是一个异步函数，接收 [`ToolContext`][agents.tool_context.ToolContext] 和采用 JSON 字符串形式的参数，并返回工具输出（例如文本、结构化工具输出对象或输出列表）。

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

### 参数与文档字符串的自动解析

如前所述，我们会自动解析函数签名以提取工具模式，并解析文档字符串以提取工具及各个参数的描述。相关注意事项如下：

1. 签名解析通过 `inspect` 模块完成。我们使用类型注解理解参数类型，并动态构建 Pydantic 模型来表示整体模式。它支持大多数类型，包括 Python 基本类型、Pydantic 模型、TypedDict 等。
2. 我们使用 `griffe` 解析文档字符串。支持的文档字符串格式包括 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测文档字符串格式，但这只能尽力而为，你可以在调用 `function_tool` 时显式设置格式。也可以将 `use_docstring_info` 设置为 `False`，以禁用文档字符串解析。

模式提取代码位于 [`agents.function_schema`][]。

### 使用 Pydantic Field 约束和描述参数

你可以使用 Pydantic 的 [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) 为工具参数添加约束（例如数字的最小值/最大值、字符串的长度或模式）和描述。与 Pydantic 一样，支持两种形式：基于默认值的形式（`arg: int = Field(..., ge=1)`）和 `Annotated` 形式（`arg: Annotated[int, Field(..., ge=1)]`）。生成的 JSON 模式和验证都会包含这些约束。

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

你可以使用 `@function_tool(timeout=...)` 为异步工具调用设置单次调用超时。

```python
import asyncio
from agents import Agent, function_tool


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

达到超时时间时，默认行为是 `timeout_behavior="error_as_result"`，它会发送一条模型可见的超时消息（例如 `Tool 'slow_lookup' timed out after 2 seconds.`）。

你可以控制超时处理方式：

-   `timeout_behavior="error_as_result"`（默认）：向模型返回超时消息，使其能够恢复。
-   `timeout_behavior="raise_exception"`：引发 [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] 并使运行失败。
-   `timeout_error_function=...`：使用 `error_as_result` 时自定义超时消息。

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

    仅异步 `@function_tool` 处理程序支持超时配置。

### 工具调用中的错误处理

通过 `@function_tool` 创建工具调用时，你可以传入 `failure_error_function`。如果工具调用崩溃，该函数会向LLM提供错误响应。

-   默认情况下（即不传入任何内容时），它会运行 `default_tool_error_function`，告知LLM发生了错误。
-   如果传入你自己的错误函数，则会改为运行该函数，并将响应发送给LLM。
-   如果显式传入 `None`，则会重新引发任何工具调用错误，供你处理。如果模型生成了无效 JSON，这可能是 `ModelBehaviorError`；如果你的代码崩溃，则可能是 `UserError` 等。

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

如果你手动创建 `FunctionTool` 对象，则必须在 `on_invoke_tool` 函数内处理错误。

## Agents as tools

在某些工作流中，你可能希望由一个中央智能体编排由多个专用智能体组成的网络，而不是转移控制权。你可以通过将智能体建模为工具来实现这一点。

```python
import asyncio

from agents import Agent, Runner

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
        "You are a translation agent. You use the tools given to you to translate. "
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


if __name__ == "__main__":
    asyncio.run(main())
```

### 工具智能体自定义

`agent.as_tool` 函数是一种便捷方法，可轻松将智能体转换为工具。它支持常见的运行时选项，例如 `max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session` 和 `needs_approval`。它还通过 `parameters`、`input_builder` 和 `include_input_schema` 支持结构化输入。

状态选项用于配置由工具调用启动的嵌套智能体运行；父运行的对话状态不会自动继承。若要在父运行和嵌套运行之间共享由客户端管理的历史记录，请显式向两者传入相同的 `session`。与 `Runner.run` 一样，请为嵌套运行选择一种状态策略：由客户端管理的 `session`，或通过 `previous_response_id` 或 `conversation_id` 在服务端管理的延续。

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

默认情况下，`Agent.as_tool()` 需要单个字符串输入（`{"input": "..."}`），但你可以通过传入 `parameters`（Pydantic 模型或数据类类型）公开结构化模式。

其他选项：

- `include_input_schema=True` 在生成的嵌套输入中包含完整的 JSON Schema。
- `input_builder=...` 让你可以完全自定义如何将结构化工具参数转换为嵌套智能体输入。
- `RunContextWrapper.tool_input` 包含嵌套运行上下文中已解析的结构化载荷。

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

有关完整的可运行代码示例，请参阅 `examples/agent_patterns/agents_as_tools_structured.py`。

### 工具智能体的审批关卡

`Agent.as_tool(..., needs_approval=...)` 使用与 `function_tool` 相同的审批流程。如果需要审批，运行会暂停，待处理条目会出现在 `result.interruptions` 中；然后使用 `result.to_state()`，并在调用 `state.approve(...)` 或 `state.reject(...)` 后恢复运行。有关完整的暂停/恢复模式，请参阅[人工介入指南](human_in_the_loop.md)。

### 自定义输出提取

在某些情况下，你可能希望先修改工具智能体的输出，再将其返回给中央智能体。以下情况可能适合这样做：

-   从子智能体的聊天历史记录中提取特定信息（例如 JSON 载荷）。
-   转换或重新格式化智能体的最终答案（例如将 Markdown 转换为纯文本或 CSV）。
-   验证输出，或在智能体响应缺失或格式错误时提供回退值。

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

在自定义提取器中，嵌套的 [`RunResult`][agents.result.RunResult] 还会公开 [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]。当你在后处理嵌套结果时需要外层工具名称、调用 ID 或原始参数，此属性非常有用。请参阅[结果指南](results.md#agent-as-tool-metadata)。

### 嵌套智能体运行的流式传输

向 `as_tool` 传入 `on_stream` 回调，即可监听嵌套智能体发出的流式事件，同时在流结束后仍返回其最终输出。

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
- 提供 `on_stream` 会自动以流式传输模式运行嵌套智能体，并在返回最终输出前耗尽整个流。
- 处理程序可以是同步或异步的；每个事件都会按照到达顺序传递。
- 通过模型工具调用来调用工具时会提供 `tool_call`；直接调用时，它可能为 `None`。
- 有关完整的可运行代码示例，请参阅 `examples/agent_patterns/agents_as_tools_streaming.py`。

### 工具的条件启用

你可以使用 `is_enabled` 参数，在运行时有条件地启用或禁用智能体工具。这样，你就可以根据上下文、用户偏好或运行时条件，动态筛选LLM可用的工具。

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

禁用的工具会在运行时对LLM完全隐藏，因此适用于：

-   根据用户权限控制功能
-   特定环境的工具可用性（开发环境与生产环境）
-   对不同工具配置进行 A/B 测试
-   根据运行时状态动态筛选工具

## 实验性功能：Codex 工具

`codex_tool` 封装了 Codex CLI，使智能体能够在工具调用期间运行限定于工作区的任务（Shell、文件编辑、MCP工具）。此功能处于实验阶段，可能会发生变化。

当你希望主智能体将范围明确的工作区任务委派给 Codex，同时不退出当前运行时，请使用此工具。默认工具名称为 `codex`。如果设置自定义名称，则该名称必须是 `codex` 或以 `codex_` 开头。当智能体包含多个 Codex 工具时，每个工具必须使用唯一名称。

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
                model="gpt-5.5",
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

请从以下选项组开始：

-   执行范围：`sandbox_mode` 和 `working_directory` 定义 Codex 可以在哪里操作。请将两者配合使用；如果工作目录不在 Git 仓库内，请设置 `skip_git_repo_check=True`。
-   线程默认值：`default_thread_options=ThreadOptions(...)` 用于配置模型、推理强度、审批策略、其他目录、网络访问和网络检索模式。应优先使用 `web_search_mode`，而不是旧版的 `web_search_enabled`。
-   轮次默认值：`default_turn_options=TurnOptions(...)` 用于配置每轮行为，例如 `idle_timeout_seconds` 和可选的取消 `signal`。
-   工具输入/输出：工具调用必须至少包含一个 `inputs` 条目，其格式为 `{ "type": "text", "text": ... }` 或 `{ "type": "local_image", "path": ... }`。`output_schema` 可用于要求 Codex 提供结构化响应。

线程复用和持久化是独立的控制项：

-   `persist_session=True` 会让对同一工具实例的重复调用复用一个 Codex 线程。
-   `use_run_context_thread_id=True` 会在运行上下文中存储并复用线程 ID，适用于共享同一可变上下文对象的多次运行。
-   线程 ID 的优先级依次为：每次调用的 `thread_id`、运行上下文线程 ID（如果启用），然后是已配置的 `thread_id` 选项。
-   对于 `name="codex"`，默认运行上下文键为 `codex_thread_id`；对于 `name="codex_<suffix>"`，则为 `codex_thread_id_<suffix>`。可以使用 `run_context_thread_id_key` 覆盖它。

运行时配置：

-   身份验证：设置 `CODEX_API_KEY`（首选）或 `OPENAI_API_KEY`，或传入 `codex_options={"api_key": "..."}`。
-   运行时：`codex_options.base_url` 会覆盖 CLI 的基础 URL。
-   二进制文件解析：设置 `codex_options.codex_path_override`（或 `CODEX_PATH`）以固定 CLI 路径。否则，SDK 会先从 `PATH` 中解析 `codex`，然后回退到捆绑的供应商二进制文件。
-   环境：`codex_options.env` 完全控制子进程环境。提供该选项时，子进程不会继承 `os.environ`。
-   流限制：`codex_options.codex_subprocess_stream_limit_bytes`（或 `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`）控制 stdout/stderr 读取器限制。有效范围为 `65536` 到 `67108864`；默认值为 `8388608`。
-   流式传输：`on_stream` 接收线程/轮次生命周期事件和条目事件（`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list` 和 `error` 条目更新）。
-   输出：结果包括 `response`、`usage` 和 `thread_id`；使用量会添加到 `RunContextWrapper.usage`。

参考资料：

-   [Codex 工具 API 参考](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions 参考](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions 参考](ref/extensions/experimental/codex/turn_options.md)
-   有关完整的可运行代码示例，请参阅 `examples/tools/codex.py` 和 `examples/tools/codex_same_thread.py`。