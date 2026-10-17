---
search:
  exclude: true
---
# 工具

工具让智能体能够执行操作，例如获取数据、运行代码、调用外部 API，甚至操作计算机。SDK 支持五类工具：

-   OpenAI 托管的工具：在 OpenAI 服务器上为模型执行。
-   本地/运行时执行工具：`ComputerTool` 和 `ApplyPatchTool` 始终在你的环境中运行，而 `ShellTool` 可以在本地或托管容器中运行。
-   `FunctionTool` 实例：将任意 Python 函数封装为工具。
-   Agents as tools：将智能体公开为可调用工具，而无需完整的任务转移。
-   实验性 Codex 工具：通过工具调用运行限定于工作区的 Codex 任务。

## 工具类型选择 {#choosing-a-tool-type}

可将本页用作目录，然后跳转到与你所控制的运行时相匹配的部分。

| 如果你希望…… | 从这里开始 |
| --- | --- |
| 使用由 OpenAI 管理的工具（网络检索、文件检索、Code Interpreter、托管 MCP、图像生成） | [托管工具](#hosted-tools) |
| 通过工具搜索将大型工具接口延迟到运行时加载 | [托管工具搜索](#hosted-tool-search) |
| 通过生成的 JavaScript 协调多个工具调用 | [编程式工具调用](#programmatic-tool-calling) |
| 在你自己的进程或环境中运行工具 | [本地运行时工具](#local-runtime-tools) |
| 将 Python 函数封装为工具 | [函数工具](#function-tools) |
| 让一个智能体调用另一个智能体，而不进行任务转移 | [Agents as tools](#agents-as-tools) |
| 从智能体运行限定于工作区的 Codex 任务 | [实验性 Codex 工具](#experimental-codex-tool) |

## 托管工具 {#hosted-tools}

使用 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 时，OpenAI 提供了一些内置工具：

-   [`WebSearchTool`][agents.tool.WebSearchTool] 允许智能体搜索网络。
-   [`FileSearchTool`][agents.tool.FileSearchTool] 允许从你的 OpenAI 向量存储中检索信息。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 允许 LLM 在沙箱环境中执行代码。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 将远程 MCP 服务器的工具公开给模型。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 根据提示词生成图像。
-   [`ToolSearchTool`][agents.tool.ToolSearchTool] 允许模型按需加载延迟工具、命名空间或托管 MCP 服务器。
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool] 允许模型通过生成的 JavaScript 协调符合条件的工具。

高级托管搜索选项：

-   除 `vector_store_ids` 和 `max_num_results` 外，`FileSearchTool` 还支持 `filters`、`ranking_options` 和 `include_search_results`。将 `max_num_results` 设置为 1 至 50 的整数；`None` 或零会使用提供方默认值。
-   `WebSearchTool` 支持 `filters`、`user_location`、`search_context_size`、`external_web_access`、`search_content_types` 和 `image_settings`。

```python
from agents import Agent, FileSearchTool, Runner, WebSearchTool

agent = Agent(
    name="Assistant",
    tools=[
        WebSearchTool(
            search_content_types=["image", "text"],
            image_settings={"max_results": 3, "caption": True},
        ),
        FileSearchTool(
            max_num_results=3,
            vector_store_ids=["VECTOR_STORE_ID"],
        ),
    ],
)


async def main():
    result = await Runner.run(
        agent, "Find recent images and supporting text about the Golden Gate Bridge at sunset."
    )
    print(result.final_output)
```

当网络检索应返回图像时，将 `search_content_types` 设置为包含 `"image"`；当模型还需要支持性文本结果时，也应包含 `"text"`。`image_settings.max_results` 请求正数个图像结果，而 `image_settings.caption` 会在可用时请求简短描述。当存在 `"image"` 时，SDK 会自动请求 `web_search_call.results`。这些原始结果存储在 [`RunResult.raw_responses`](results.md#raw-responses) 的 `web_search_call` 项中，与助手消息分开，并且可以包含 `image_url`、`source_website_url`、`thumbnail_url` 和 `caption`。请参阅 OpenAI 的[图像搜索结果指南](https://developers.openai.com/api/docs/guides/tools-web-search#image-search-results)。

### 托管工具搜索 {#hosted-tool-search}

工具搜索允许 OpenAI Responses 模型将大型工具接口延迟到运行时加载，使模型仅加载当前轮次所需的子集。当你有许多函数工具、命名空间组或托管 MCP 服务器，并希望减少工具架构令牌而不预先公开所有工具时，这非常有用。

如果构建智能体时已经知道候选工具，请从托管工具搜索开始。如果你的应用程序需要动态决定加载哪些内容，Responses API 也支持由客户端执行的工具搜索，但标准 `Runner` 不会自动执行该模式。

```python
from typing import Annotated

from agents import Agent, Runner, ToolSearchTool, tool_namespace
from agents.decorators import tool


@tool(defer_loading=True)
def get_customer_profile(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """Fetch a CRM customer profile."""
    return f"profile for {customer_id}"


@tool(defer_loading=True)
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

-   托管工具搜索仅适用于 OpenAI Responses 模型。
-   在智能体上配置延迟加载接口时，只添加一个 `ToolSearchTool()`。
-   可搜索接口包括 `@function_tool(defer_loading=True)`、`tool_namespace(name=..., description=..., tools=[...])` 和 `HostedMCPTool(tool_config={..., "defer_loading": True})`。
-   延迟加载的函数工具必须与 `ToolSearchTool()` 配对使用。仅使用命名空间的设置也可以使用 `ToolSearchTool()`，让模型按需加载正确的组。
-   `tool_namespace()` 将 `FunctionTool` 实例归入具有共享名称和描述的命名空间。当你有许多相关工具（例如 `crm`、`billing` 或 `shipping`）时，这通常是最佳选择。
-   OpenAI 的官方最佳实践指南建议[尽可能使用命名空间](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible)。
-   在可能的情况下，相比大量单独延迟的函数，应优先使用命名空间或托管 MCP 服务器。它们通常能为模型提供更好的高层搜索接口，并节省更多令牌。
-   命名空间可以混合包含即时工具和延迟工具。不带 `defer_loading=True` 的工具仍可立即调用，而同一命名空间中的延迟工具则通过工具搜索加载。
-   根据经验，应保持每个命名空间相对较小，最好少于 10 个函数。
-   具名 `tool_choice` 不能以未封装的命名空间名称或仅延迟工具为目标。应优先使用 `auto`、`required` 或真实的顶层可调用工具名称。
-   `ToolSearchTool(execution="client")` 用于手动 Responses 编排。如果模型发出由客户端执行的 `tool_search_call`，标准 `Runner` 会引发异常，而不会代你执行。
-   工具搜索活动会以专用项和事件类型出现在 [`RunResult.new_items`](results.md#new-items) 和 [`RunItemStreamEvent`](streaming.md#run-item-event-names) 中。
-   有关涵盖命名空间加载和顶层延迟工具的完整可运行示例，请参阅 `examples/tools/tool_search.py`。
-   官方平台指南：[工具搜索](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### 编程式工具调用 {#programmatic-tool-calling}

编程式工具调用允许受支持的 OpenAI Responses 模型生成 JavaScript，以调用符合条件的工具、合并其输出，并向模型返回一个结果。它适用于边界明确的工作流，这些工作流可以受益于循环、分支、并行调用或中间计算，而不必在每次工具调用后都与模型往返交互。

生成的程序在全新的托管 V8 环境中运行。它无法访问 Node.js API、文件系统或网络，也没有持久化进程。程序只能与显式允许的工具交互。

```python
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    ProgrammaticToolCallingTool,
    Runner,
)
from agents.decorators import tool


class InventoryOutput(BaseModel):
    sku: str
    available_units: int


@tool(allowed_callers=["programmatic"])
def get_inventory(sku: str) -> InventoryOutput:
    return InventoryOutput(sku=sku, available_units=42)


agent = Agent(
    name="Inventory planner",
    model="gpt-5.6",
    model_settings=ModelSettings(tool_choice="programmatic_tool_calling"),
    tools=[get_inventory, ProgrammaticToolCallingTool()],
)

result = Runner.run_sync(agent, "Check inventory for desk-lamp and summarize it.")
print(result.final_output)
```

注意事项：

-   编程式工具调用仅适用于受支持的 OpenAI Responses 模型。Chat Completions 模型和非 Responses 后端会拒绝 `ProgrammaticToolCallingTool()` 和 `tool_choice="programmatic_tool_calling"`。
-   每个智能体最多添加一个 `ProgrammaticToolCallingTool()`。智能体还必须公开至少一个可通过编程方式调用的工具、由命名空间、延迟函数或延迟托管 MCP 服务器支持的 `ToolSearchTool()`，或由提示词管理的不透明工具接口。没有可搜索接口的单独 `ToolSearchTool()` 会被拒绝。
-   `allowed_callers` 控制工具的调用方式。省略它时，仅允许模型直接调用。使用 `["programmatic"]` 表示仅允许程序访问，或使用 `["direct", "programmatic"]` 同时允许两种方式。
-   可以选择启用该功能的 SDK 工具类型包括 `FunctionTool`、`CustomTool`、`ShellTool`、`ApplyPatchTool`、`HostedMCPTool` 和 `CodeInterpreterTool`。函数、自定义、shell 和应用补丁工具直接公开 `allowed_callers`。对于托管 MCP 和 Code Interpreter，请在 `tool_config` 内设置 `allowed_callers`。
-   对于 `@function_tool(allowed_callers=[...])`，Pydantic 模型、TypedDict 或数据类等结构化返回注解会自动转换为严格的对象输出架构，并且返回值在返回给程序之前会根据该架构进行验证。当函数没有可用注解时，请使用 `output_type=...`；如果已经有严格的对象架构，则可使用更底层的 `output_json_schema={...}` 逃生舱口。`output_type` 和 `output_json_schema` 互斥。`str`、`Any` 或 `None` 的返回注解不会创建输出架构。对于由架构支持且归程序所有的调用，默认失败格式化程序会被禁用，因为其自由格式文本不符合输出架构。因此，除非提供返回符合架构的 JSON 的自定义 `failure_error_function`，否则处理程序异常会向上传播。
-   归程序所有的 SDK 工具仍使用常规 Runner 生命周期。工具输入和输出安全防护措施、钩子、超时、并发限制、审批、会话以及 `RunState` 暂停/恢复行为仍然适用，并且 SDK 会保留每个子调用与程序调用方的关系。
-   只要存在 `ProgrammaticToolCallingTool()`，模型请求重试就会使用更严格的重放安全边界，即使程序尚未执行。SDK 会为这些请求禁用由提供方管理的重试和 WebSocket 事件前重试。Runner 重试策略仅在提供方建议明确将重放标记为安全时才会重试；仅设置 `retry_policies.network_error()` 不会覆盖此边界。
-   对审批敏感或影响较大的工具通常更适合保留为直接调用，以便人员可以在每项操作成为更大程序的一部分之前进行审核。如果归程序所有的调用因等待审批而暂停，请通过 `RunState` 解决中断，并照常恢复原始运行。
-   编程式工具调用可以与[托管工具搜索](#hosted-tool-search)结合使用。生成的程序必须先由模型加载延迟工具，然后才能调用它们。
-   `program` 项及其普通的归程序所有的子工具调用会显示为 [`ToolCallItem`][agents.items.ToolCallItem] 条目。匹配的 `program_output` 会显示为 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]。托管 MCP 审批请求和工具目录则使用专门的 MCP 项和流事件。有关检查详情，请参阅[结果](results.md#new-items)和[流式传输](streaming.md#run-item-event-names)。
-   有关完整的并发库存规划示例，请参阅 `examples/tools/programmatic_tool_calling.py`。
-   官方平台指南：[编程式工具调用](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)。

### 托管容器 shell 与技能 {#hosted-container-shell-skills}

`ShellTool` 还支持由 OpenAI 托管的容器执行。当你希望模型在托管容器中运行 shell 命令，而不是在本地运行时中运行时，请使用此模式。

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

要在后续运行中复用现有容器，请设置 `environment={"type": "container_reference", "container_id": "cntr_..."}`。

注意事项：

-   托管 shell 可通过 Responses API shell 工具使用。
-   `container_auto` 为请求配置容器；`container_reference` 复用现有容器。
-   `container_auto` 还可以包含 `file_ids` 和 `memory_limit`。
-   `environment.skills` 接受技能引用和内联技能包。
-   使用托管环境时，不要在 `ShellTool` 上设置 `executor`、`needs_approval` 或 `on_approval`。
-   `network_policy` 支持 `disabled` 和 `allowlist` 模式。
-   在允许列表模式下，`network_policy.domain_secrets` 可以按名称注入限定于域的密钥。
-   有关完整示例，请参阅 `examples/tools/container_shell_skill_reference.py` 和 `examples/tools/container_shell_inline_skill.py`。
-   OpenAI 平台指南：[Shell](https://platform.openai.com/docs/guides/tools-shell)和[技能](https://platform.openai.com/docs/guides/tools-skills)。

## 本地运行时工具 {#local-runtime-tools}

本地运行时工具在模型响应本身之外执行。模型仍会决定何时调用它们，但实际工作由你的应用程序或配置的执行环境完成。

`ComputerTool` 和 `ApplyPatchTool` 始终需要你提供本地实现。`ShellTool` 涵盖两种模式：需要托管执行时，使用上面的托管容器配置；需要在你自己的进程中运行命令时，使用下面的本地运行时配置。

本地运行时工具要求你提供实现：

-   [`ComputerTool`][agents.tool.ComputerTool]：实现 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 接口，以启用 GUI/浏览器自动化。
-   [`ShellTool`][agents.tool.ShellTool]：用于本地执行和托管容器执行的最新 shell 工具。
-   [`LocalShellTool`][agents.tool.LocalShellTool]：旧版本地 shell 集成。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：实现 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor]，以在本地应用差异。
-   使用 `ShellTool(environment={"type": "local", "skills": [...]})` 可启用本地 shell 技能。

Shell 操作超时使用正整数毫秒值表示有限超时。在调用本地 `ShellTool` 执行器之前，SDK 会将 `0` 和 `None` 都视为未显式设置超时，因为零在不同执行器实现中没有可移植的统一含义；其他值会在调用执行器前被拒绝。这仅适用于超时字段：`max_output_length=0` 仍是受支持的空捕获输出请求。

### 本地 shell 与文件编辑审批 {#approval-for-local-shell-and-file-edits}

本地 `ShellTool` 和 `ApplyPatchTool` 默认使用 `needs_approval=False`。使用此设置时，SDK 可以调用你的执行器或编辑器，而无需请求审批。你的实现决定命令的运行位置或文件的更改位置，并且必须强制执行预期的资源权限和隔离；SDK 审批并不提供沙箱。

对于需要审核的命令或文件编辑，请在工具上设置 `needs_approval=True`，或提供一个可调用策略，让它为需要审批的调用返回 `True`。如果没有 `on_approval` 回调，运行会在调用执行器或编辑器之前暂停，并在 `result.interruptions` 中返回待处理请求。通过 `RunState` 批准或拒绝这些请求，然后按照[人在回路指南](human_in_the_loop.md)中的说明恢复运行。

要在应用程序代码中立即作出决定，请同时设置 `needs_approval` 和 `on_approval`。仅当调用需要审批且没有现有审批决定时，SDK 才会调用 `on_approval`；仅设置回调不会启用审批。有关 CLI 提示，请参阅 `examples/tools/shell.py`；有关手动中断处理，请参阅 `examples/tools/shell_human_in_the_loop.py`。`examples/tools/apply_patch.py` 示例则会在更改文件前，在其编辑器内显示提示。

本地 `ShellTool` 和 `ApplyPatchTool` 的持久化决定可以跨任务转移延续到另一个智能体上类型和名称均相同的工具，也可以跨复用同一 `RunContextWrapper` 的不同运行延续。当执行器或编辑器权限不同时，请使用逐调用决定；使用不同执行权限开始新运行时，请使用新的包装器。有关详细信息，请参阅[审批范围警告](human_in_the_loop.md#how-the-approval-flow-works)，其中包括为何逐调用决定和恢复期间的上下文覆盖不会清除已存储的审批。

当你的应用程序有意授权自动执行时，请保留 `needs_approval=False`，例如使用强制执行你的沙箱策略的执行器。托管 shell 环境不支持 SDK 的本地 `needs_approval` 或 `on_approval` 设置。

### ComputerTool 与 Responses 计算机工具 {#computertool-and-the-responses-computer-tool}

`ComputerTool` 仍是本地执行框架：你提供 [`Computer`][agents.computer.Computer] 或 [`AsyncComputer`][agents.computer.AsyncComputer] 实现，SDK 则将该执行框架映射到 OpenAI Responses API 的计算机操作接口。

当 [`Agent`][agents.agent.Agent] 未设置 `model` 时，会应用常规的 SDK 模型选择优先级。SDK 当前的内置默认模型 [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) 支持计算机操作。如果 `OPENAI_DEFAULT_MODEL` 或 `RunConfig.model` 覆盖了该默认值，请选择支持计算机操作的模型。如果希望为计算机操作工作负载选择不同的能力和成本配置，请在智能体上设置 `model`。下面的示例使用 [`gpt-5.6`](https://developers.openai.com/api/docs/models/gpt-5.6) 别名，OpenAI 会将其路由到 GPT-5.6 Sol；你也可以选择其他支持计算机操作的模型，例如 [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) 或 [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

对于向支持正式发布版内置计算机工具的模型（例如 `gpt-5.6`）发出的显式请求，SDK 会发送载荷 `{"type": "computer"}`。对于向旧版 `computer-use-preview` 模型发出的请求，SDK 会继续发送预览版载荷 `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}`。这与平台的[从 `computer-use-preview` 迁移](https://developers.openai.com/api/docs/guides/tools-computer-use-integration#migration-from-computer-use-preview)保持一致：

-   模型：`computer-use-preview` -> `gpt-5.6-sol`
-   工具选择器：`computer_use_preview` -> `computer`
-   计算机调用结构：每个 `computer_call` 对应一个 `action` -> `computer_call` 上的批量 `actions[]`
-   截断：预览版路径要求 `ModelSettings(truncation="auto")` -> 正式发布版路径不要求

SDK 根据实际 Responses 请求中的有效模型选择传输结构。如果你使用提示词模板，并且因为提示词拥有模型配置而使请求省略 `model`，SDK 会保留与预览版兼容的计算机载荷，除非你显式指定受支持的正式发布版模型（例如 `model="gpt-5.6"`），或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制选择正式发布版。

当存在 [`ComputerTool`][agents.tool.ComputerTool] 时，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 都会被接受，并规范化为与有效请求模型匹配的内置选择器。如果没有 `ComputerTool`，这些字符串仍会被视为普通函数名称。

当 `ComputerTool` 由 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂支持时，这一区别很重要。正式发布版 `computer` 载荷在序列化时不需要 `environment` 或尺寸，因此可以在工厂生成 `Computer` 或 `AsyncComputer` 实例之前进行序列化。与预览版兼容的序列化仍需要已解析的 `Computer` 或 `AsyncComputer` 实例，以便 SDK 发送 `environment`、`display_width` 和 `display_height`。

在运行时，两条路径仍使用相同的本地执行框架。预览版响应会发出带有单个 `action` 的 `computer_call` 项；正式发布版响应可以发出批量 `actions[]`，SDK 会按顺序执行它们，然后生成 `computer_call_output` 屏幕截图项。有关基于 Playwright 的可运行执行框架，请参阅 `examples/tools/computer_use.py`。

```python
from agents import Agent, ApplyPatchTool, ComputerTool, ShellTool
from agents.computer import AsyncComputer
from agents.editor import ApplyPatchResult, ApplyPatchOperation, ApplyPatchEditor


class NoopComputer(AsyncComputer):
    environment = "browser"
    dimensions = (1024, 768)

    async def screenshot(self):
        return ""

    async def click(self, x, y, button): ...
    async def double_click(self, x, y): ...
    async def scroll(self, x, y, scroll_x, scroll_y): ...
    async def type(self, text): ...
    async def wait(self): ...
    async def move(self, x, y): ...
    async def keypress(self, keys): ...
    async def drag(self, path): ...


class NoopEditor(ApplyPatchEditor):
    async def create_file(self, op: ApplyPatchOperation):
        return ApplyPatchResult(status="completed")

    async def update_file(self, op: ApplyPatchOperation):
        return ApplyPatchResult(status="completed")

    async def delete_file(self, op: ApplyPatchOperation):
        return ApplyPatchResult(status="completed")


async def run_shell(request):
    return "shell output"


agent = Agent(
    name="Local tools agent",
    tools=[
        ShellTool(executor=run_shell),
        ApplyPatchTool(editor=NoopEditor()),
        ComputerTool(computer=NoopComputer()),
    ],
    # Optional: omit this argument to use the configured or built-in default model.
    model="gpt-5.6",
)
```

#### 待处理的计算机安全检查 {#pending-computer-safety-checks}

Responses API 可以在 `computer_call` 中包含 `pending_safety_checks`，以标记需要你的应用程序审核的疑虑。每项检查都有一个 `id`，以及用于描述疑虑类型并提供详情的可选 `code` 和 `message` 字段。这些检查来自 API；Agents SDK 会将其传递给你的回调。有关 `pending_safety_checks` 和 `acknowledged_safety_checks`，请参阅 [Responses API 参考](https://developers.openai.com/api/reference/resources/responses/methods/create)；有关应用程序级预防措施，请参阅[计算机操作安全指南](https://developers.openai.com/api/docs/guides/tools-computer-use#run-safely)。

配置 [`ComputerTool.on_safety_check`][agents.tool.ComputerTool.on_safety_check]，以决定是否继续执行被标记的计算机调用。SDK 仅在调用包含待处理检查时，才会在执行该调用的操作前调用此回调。

回调每次接收一个 [`ComputerToolSafetyCheckData`][agents.tool.ComputerToolSafetyCheckData]。返回 `True` 表示确认该检查。返回 `False` 会在执行该调用的操作或屏幕截图前引发 [`UserError`][agents.exceptions.UserError]。配置回调后，SDK 要求确认所有报告的检查。回调可以是同步或异步的。

如果省略 `on_safety_check`，SDK 会继续执行并将检查保留为未确认状态。SDK 不会自动提示人员。应用程序需要审核被标记的调用时，请显式配置回调。例如，以下保守策略会拒绝所有被标记的调用：

```python
from agents import AsyncComputer, Computer, ComputerTool
from agents.tool import ComputerToolSafetyCheckData


def reject_flagged_call(data: ComputerToolSafetyCheckData) -> bool:
    return False


def computer_tool_with_safety_checks(computer: Computer | AsyncComputer) -> ComputerTool:
    return ComputerTool(computer=computer, on_safety_check=reject_flagged_call)
```

要允许经过审核的调用，请将 `reject_flagged_call` 替换为一个回调，该回调向应用程序的审核人员展示 `data.safety_check` 和拟执行的 `data.tool_call`，等待决定并返回该决定。未知的检查代码也应视为需要审核。

没有待处理检查的计算机调用不会触发此回调。对于必须应用于每项计算机操作的策略，请在你的计算机执行框架中强制执行。要收集人员的决定，你的回调必须安排相应交互并返回决定；SDK 不会填充 `RunResult.interruptions`，也不会使用[人在回路](human_in_the_loop.md)中所述的暂停和恢复流程。

## 函数工具 {#function-tools}

你可以将任意 Python 函数用作工具。Agents SDK 会自动设置该工具：

-   工具名称将是 Python 函数的名称（你也可以提供名称）
-   工具描述将取自函数的文档字符串（你也可以提供描述）
-   函数输入的架构会根据函数参数自动创建
-   除非禁用，否则每项输入的描述会取自函数的文档字符串

由 `@tool` 创建的工具通过只读 `__wrapped__` 属性公开原始 Python 可调用对象。这对于检查和测试很有用，但直接调用它会绕过工具运行时管线，包括架构验证、上下文注入、安全防护措施、超时、失败处理和追踪。手动构建的 `FunctionTool` 实例不会公开 `__wrapped__`。

我们使用 Python 的 `inspect` 模块提取函数签名，同时使用 [`griffe`](https://mkdocstrings.github.io/griffe/) 解析文档字符串，并使用 `pydantic` 创建架构。

使用 OpenAI Responses 模型时，`@function_tool(defer_loading=True)` 会隐藏函数工具，直到 `ToolSearchTool()` 加载它。你还可以使用 [`tool_namespace()`][agents.tool.tool_namespace] 对相关函数工具进行分组。有关完整设置和约束，请参阅[托管工具搜索](#hosted-tool-search)。

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper
from agents.decorators import tool


class Location(TypedDict):
    lat: float
    long: float


@tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@tool(name_override="fetch_data")  # (3)!
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

1.  你可以将任意 Python 类型用作函数参数，并且函数可以是同步或异步的。
2.  如果存在文档字符串，它们会用于获取描述和参数描述。
3.  函数可以选择将运行上下文作为第一个参数。你还可以设置覆盖项，例如工具名称、描述、要使用的文档字符串样式等。
4.  你可以将已装饰的函数传入工具列表。

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

### 从函数工具返回图像或文件 {#returning-images-or-files-from-function-tools}

除了返回文本输出外，你还可以将一张或多张图像或一个或多个文件作为函数工具的输出返回。为此，你可以返回以下任意内容：

-   图像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（或 TypedDict 版本 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   文件：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（或 TypedDict 版本 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   文本：字符串、可转换为字符串的对象，或 [`ToolOutputText`][agents.tool.ToolOutputText]（或 TypedDict 版本 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### 自定义函数工具 {#custom-function-tools}

有时，你可能不希望将 Python 函数用作工具。如果愿意，可以直接创建 [`FunctionTool`][agents.tool.FunctionTool]。你需要提供：

-   `name`
-   `description`
-   `params_json_schema`，即参数的 JSON 架构
-   `on_invoke_tool`，即一个异步函数，它接收 [`ToolContext`][agents.tool_context.ToolContext] 和 JSON 字符串形式的参数，并返回工具输出（例如文本、结构化工具输出对象或输出列表）。

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

### 参数与文档字符串的自动解析 {#automatic-argument-and-docstring-parsing}

如前所述，我们会自动解析函数签名以提取工具架构，并解析文档字符串以提取工具及各个参数的描述。相关注意事项如下：

1. 签名解析通过 `inspect` 模块完成。我们使用类型注解了解参数类型，并动态构建一个 Pydantic 模型来表示整体架构。它支持大多数类型，包括 Python 基本类型、Pydantic 模型、TypedDict 等。
2. 我们使用 `griffe` 解析文档字符串。支持的文档字符串格式为 `google`、`sphinx` 和 `numpy`。我们会尝试自动检测文档字符串格式，但这是尽力而为的行为，你可以在调用 `function_tool` 时显式设置格式。你也可以将 `use_docstring_info` 设置为 `False`，以禁用文档字符串解析。对于 Google 风格的文档字符串，解析器还接受紧接摘要文本、且中间没有空行的 `Args:`、`Arguments:`、`Params:` 或 `Parameters:` 部分。

架构提取代码位于 [`agents.function_schema`][] 中。

### 使用 Pydantic Field 约束和描述参数 {#constraining-and-describing-arguments-with-pydantic-field}

你可以使用 Pydantic 的 [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) 为工具参数添加约束（例如数字的最小值/最大值、字符串的长度或模式）和描述。与 Pydantic 一样，两种形式都受支持：基于默认值的形式（`arg: int = Field(..., ge=1)`）和 `Annotated`（`arg: Annotated[int, Field(..., ge=1)]`）。生成的 JSON 架构和验证会包含这些约束。

对于可变参数，注解描述每个收集到的值。因此，SDK 会将 `Annotated[..., Field(...)]` 约束应用于通过 `*args` 或 `**kwargs` 提供的每个值，而省略的可变参数仍是有效的空集合。

SDK 会忽略可变参数（`*args` 或 `**kwargs`）注解中的 `Field(description=...)`。要描述收集后的参数，请在函数文档字符串中使用参数条目，或在 `Annotated` 中使用字符串，例如 `*scores: Annotated[int, "Exam scores", Field(ge=0, le=100)]`。启用文档字符串解析且两个来源均提供描述时，文档字符串中的描述优先。

对于 `**kwargs`，请使用 `@tool(strict_mode=False)`，并在以参数命名的嵌套对象中提供关键字值。例如，具有 `**scores: int` 的工具会接收 `{"scores": {"exam": 90}}`。

将标量位置值注解为 `*args: T`。如果每个位置值本身都是同构元组，请使用 `*args: tuple[T, ...]`；SDK 会拒绝 `*args: tuple[int, str]` 等固定长度元组注解，因为单一固定元组结构无法描述位置值的可变序列。

```python
from typing import Annotated
from pydantic import Field
from agents.decorators import tool


# Default-based form
@tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"


# Annotated form
@tool
def score_b(
    score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")],
) -> str:
    return f"Score recorded: {score}"
```

### 函数工具超时 {#function-tool-timeouts}

你可以使用 `@function_tool(timeout=...)` 为异步函数工具设置逐调用超时。

```python
import asyncio
from agents import Agent
from agents.decorators import tool


@tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

达到超时时，默认行为是 `timeout_behavior="error_as_result"`，它会发送一条模型可见的超时消息（例如 `Tool 'slow_lookup' timed out after 2 seconds.`）。

你可以控制超时处理方式：

-   `timeout_behavior="error_as_result"`（默认）：向模型返回超时消息，使其能够恢复。
-   `timeout_behavior="raise_exception"`：引发 [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] 并使运行失败。
-   `timeout_error_function=...`：使用 `error_as_result` 时自定义超时消息。

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError
from agents.decorators import tool


@tool(timeout=1.5, timeout_behavior="raise_exception")
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

### 函数工具错误处理 {#handling-errors-in-function-tools}

通过 `@function_tool` 创建函数工具时，可以传入 `failure_error_function`。这是一个函数，用于在工具调用崩溃时向 LLM 提供错误响应。

-   默认情况下（即不传入任何内容时），它会运行 `default_tool_error_function`，告知 LLM 发生了错误。
-   如果传入自定义错误函数，则会改为运行该函数，并将响应发送给 LLM。
-   如果显式传入 `None`，任何工具调用错误都会重新引发，供你处理。如果模型生成了无效 JSON，错误可能是 `ModelBehaviorError`；如果你的代码崩溃，错误可能是 `UserError`，等等。

```python
from agents import RunContextWrapper
from agents.decorators import tool
from typing import Any


def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."


@tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
    This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(
            f"Could not retrieve profile for user_id: {user_id}. API returned an error."
        )
```

如果手动创建 `FunctionTool` 对象，则必须在 `on_invoke_tool` 函数内部处理错误。

## Agents as tools {#agents-as-tools}

在某些工作流中，你可能希望由一个中央智能体编排专业智能体网络，而不是转移控制权。你可以通过将智能体建模为工具来实现这一点。

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

### 工具智能体自定义 {#customizing-tool-agents}

`agent.as_tool` 是一种将智能体转换为工具的便捷方法。它支持常见的运行时选项，例如 `max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session` 和 `needs_approval`。它还支持通过 `parameters`、`input_builder` 和 `include_input_schema` 使用结构化输入。

状态选项用于配置由工具调用启动的嵌套智能体运行；父运行的对话状态不会自动继承。要在父运行与嵌套运行之间共享由客户端管理的历史记录，请显式向两者传入同一个 `session`。与 `Runner.run` 一样，请为嵌套运行选择一种状态策略：由客户端管理的 `session`，或通过 `previous_response_id` 或 `conversation_id` 进行由服务器管理的续接。

```python
from agents.decorators import tool


@tool
async def run_my_agent() -> str:
    """A tool that runs the agent with custom configs"""

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(agent, input="...", max_turns=5, run_config=...)

    return str(result.final_output)
```

### 工具智能体的结构化输入 {#structured-input-for-tool-agents}

默认情况下，`Agent.as_tool()` 需要一个包含单个字符串字段 `input`（`{"input": "..."}`）的对象，但你可以通过传入 `parameters`（Pydantic 模型类型或数据类类型）来公开结构化架构。

其他选项：

- `include_input_schema=True` 在生成的嵌套输入中包含完整的 JSON 架构。
- `input_builder=...` 允许你完全自定义如何将结构化工具参数转换为嵌套智能体输入。
- `RunContextWrapper.tool_input` 在嵌套运行上下文中包含已解析的结构化载荷。

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

有关完整的可运行示例，请参阅 `examples/agent_patterns/agents_as_tools_structured.py`。

### 工具智能体的审批关卡 {#approval-gates-for-tool-agents}

`Agent.as_tool(..., needs_approval=...)` 使用与 `function_tool` 相同的审批流程。如果需要审批，运行会暂停，待处理项会出现在 `result.interruptions` 中；随后使用 `result.to_state()`，并在调用 `state.approve(...)` 或 `state.reject(...)` 后恢复。有关完整的暂停/恢复模式，请参阅[人在回路指南](human_in_the_loop.md)。

### 自定义输出提取 {#custom-output-extraction}

在某些情况下，你可能希望先修改工具智能体的输出，再将其返回给中央智能体。这可能适用于以下情况：

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

在自定义提取器内部，嵌套的 [`RunResult`][agents.result.RunResult] 还会公开 [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]，当你需要在后处理嵌套结果时获取外层工具名称、调用 ID 或原始参数，这非常有用。请参阅[结果指南](results.md#agent-as-tool-metadata)。

### 嵌套智能体运行的流式传输 {#streaming-nested-agent-runs}

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
- 提供 `on_stream` 后，会自动以流式模式运行嵌套智能体，并在返回最终输出前排空流。
- 处理程序可以是同步或异步的；每个事件都会在到达时按顺序传递。
- 通过模型工具调用来调用工具时，`tool_call` 会存在；直接调用时，它可能是 `None`。
- 有关完整的可运行示例，请参阅 `examples/agent_patterns/agents_as_tools_streaming.py`。

### 条件式工具启用 {#conditional-tool-enabling}

你可以使用 `is_enabled` 参数，在运行时有条件地启用或禁用智能体工具。这样可以根据上下文、用户偏好或运行时条件，动态筛选哪些工具可供 LLM 使用。

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
    context = LanguageContext(language_preference="french_spanish")
    result = await Runner.run(orchestrator, "How are you?", context=context)
    print(result.final_output)


asyncio.run(main())
```

`is_enabled` 参数接受：

-   **布尔值**：`True`（始终启用）或 `False`（始终禁用）
-   **可调用函数**：接收 `(context, agent)` 并返回布尔值的函数
-   **异步函数**：用于复杂条件逻辑的异步函数

禁用的工具在运行时对 LLM 完全隐藏，因此适用于：

-   请求范围内的能力可见性
-   特定环境的工具可用性（开发环境与生产环境）
-   对不同工具配置进行 A/B 测试
-   根据运行时状态动态筛选工具

对于本地配置的函数工具，Runner 还会在调用前重新评估 `is_enabled`。但是，`is_enabled` 控制可见性和分派；它不能替代依赖工具参数或所访问资源的授权。请在工具实现内部执行这些检查，或在适当情况下使用[工具输入安全防护措施](guardrails.md#tool-guardrails)和[审批](human_in_the_loop.md)。MCP 服务器必须自行对受保护的操作进行授权。

有关将单一应用程序策略应用于函数工具、MCP 工具和任务转移的模式，请参阅[上下文管理](context.md#use-local-context-for-capability-visibility)。

## 实验性 Codex 工具 {#experimental-codex-tool}

`codex_tool` 封装 Codex CLI，使智能体可以在工具调用期间运行限定于工作区的任务（shell、文件编辑、MCP 工具）。此接口属于实验性功能，可能会发生变化。

当你希望主智能体在不离开当前运行的情况下，将边界明确的工作区任务委托给 Codex 时，请使用它。默认工具名称是 `codex`。如果设置自定义名称，则该名称必须是 `codex` 或以 `codex_` 开头。当一个智能体包含多个 Codex 工具时，每个工具都必须使用唯一名称。

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

-   执行范围：`sandbox_mode` 和 `working_directory` 定义 Codex 可以操作的位置。请将二者配对使用，并在工作目录不位于 Git 仓库中时设置 `skip_git_repo_check=True`。
-   线程默认值：`default_thread_options=ThreadOptions(...)` 配置模型、推理强度、审批策略、附加目录、网络访问和网络检索模式。应优先使用 `web_search_mode`，而不是旧版 `web_search_enabled`。
-   轮次默认值：`default_turn_options=TurnOptions(...)` 配置逐轮行为，例如 `idle_timeout_seconds` 和可选的取消 `signal`。
-   工具输入/输出：工具调用必须至少包含一个带有 `{ "type": "text", "text": ... }` 或 `{ "type": "local_image", "path": ... }` 的 `inputs` 项。`output_schema` 允许你要求 Codex 返回结构化响应。

线程复用与持久化是两个独立控制项：

-   `persist_session=True` 为对同一工具实例的重复调用复用一个 Codex 线程。
-   `use_run_context_thread_id=True` 会跨共享同一可变上下文对象的运行，在运行上下文中存储并复用线程 ID。
-   线程 ID 的优先级依次为：逐调用的 `thread_id`、运行上下文线程 ID（如果启用），然后是已配置的 `thread_id` 选项。
-   对于 `name="codex"`，默认运行上下文键为 `codex_thread_id`；对于 `name="codex_<suffix>"`，默认运行上下文键为 `codex_thread_id_<suffix>`。使用 `run_context_thread_id_key` 可覆盖它。

运行时配置：

-   身份验证：设置 `CODEX_API_KEY`（首选）或 `OPENAI_API_KEY`，或者传入 `codex_options={"api_key": "..."}`。
-   运行时：`codex_options.base_url` 覆盖 CLI 基础 URL。
-   二进制文件解析：设置 `codex_options.codex_path_override`（或 `CODEX_PATH`）以固定 CLI 路径。否则，SDK 会先从 `PATH` 解析 `codex`，然后回退到捆绑的供应商二进制文件。
-   环境：`codex_options.env` 完全控制子进程环境。提供该选项后，子进程不会继承 `os.environ`。
-   流限制：`codex_options.codex_subprocess_stream_limit_bytes`（或 `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`）控制 stdout/stderr 读取器限制。有效范围为 `65536` 至 `67108864`；默认值为 `8388608`。
-   流式传输：`on_stream` 接收线程/轮次生命周期事件和项事件（`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list` 和 `error` 项更新）。
-   输出：结果包含 `response`、`usage` 和 `thread_id`；用量会添加到 `RunContextWrapper.usage`。

参考：

-   [Codex 工具 API 参考](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions 参考](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions 参考](ref/extensions/experimental/codex/turn_options.md)
-   有关完整的可运行示例，请参阅 `examples/tools/codex.py` 和 `examples/tools/codex_same_thread.py`。