---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委派给另一个智能体。这在不同智能体分别专注于不同领域的场景中尤其有用。例如，客户支持应用可能包含多个智能体，分别专门处理订单状态、退款、常见问题等任务。

任务转移会以工具的形式呈现给LLM。因此，如果要将任务转移给名为 `Refund Agent` 的智能体，该工具将被命名为 `transfer_to_refund_agent`。

## 任务转移的创建

所有智能体都有一个 [`handoffs`][agents.agent.Agent.handoffs] 参数，它既可以直接接受 `Agent`，也可以接受用于自定义任务转移的 `Handoff` 对象。

如果传入普通的 `Agent` 实例，其 [`handoff_description`][agents.agent.Agent.handoff_description]（如果已设置）将附加到默认工具描述中。可以使用它来提示模型何时应选择该任务转移，而无须编写完整的 `handoff()` 对象。

你可以使用Agents SDK提供的 [`handoff()`][agents.handoffs.handoff] 函数创建任务转移。此函数允许你指定任务要转移到的智能体，以及可选的覆盖项和输入过滤器。

### 基本用法

以下是创建简单任务转移的方法：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 你可以直接使用智能体（如 `billing_agent`），也可以使用 `handoff()` 函数。

### 通过 `handoff()` 函数自定义任务转移

[`handoff()`][agents.handoffs.handoff] 函数允许你自定义任务转移。

-   `agent`：任务将转移到的智能体。
-   `tool_name_override`：默认使用 `Handoff.default_tool_name()` 函数，其结果为 `transfer_to_<agent_name>`。你可以覆盖此设置。
-   `tool_description_override`：覆盖来自 `Handoff.default_tool_description()` 的默认工具描述。
-   `on_handoff`：调用任务转移时执行的回调函数。它适用于在确认调用任务转移后立即启动数据获取等操作。此函数接收智能体上下文，也可以选择接收LLM生成的输入。输入数据由 `input_type` 参数控制。
-   `input_type`：任务转移工具调用参数的架构。设置后，解析后的有效负载会传递给 `on_handoff`。
-   `input_filter`：用于过滤下一个智能体接收的输入。更多信息请参见下文。
-   `is_enabled`：是否启用任务转移。它可以是布尔值，也可以是返回布尔值的函数，因此你可以在运行时动态启用或禁用任务转移。
-   `nest_handoff_history`：对 RunConfig 级别 `nest_handoff_history` 设置的可选单次调用覆盖。如果为 `None`，则改用当前运行配置中定义的值。

[`handoff()`][agents.handoffs.handoff] 辅助函数始终会将控制权转移给你传入的特定 `agent`。如果存在多个可能的目标，请为每个目标注册一个任务转移，并让模型从中选择。只有当你自己的任务转移代码必须在调用时决定返回哪个智能体时，才应使用自定义的 [`Handoff`][agents.handoffs.Handoff]。

```python
from agents import Agent, handoff, RunContextWrapper

def on_handoff(ctx: RunContextWrapper[None]):
    print("Handoff called")

agent = Agent(name="My agent")

handoff_obj = handoff(
    agent=agent,
    on_handoff=on_handoff,
    tool_name_override="custom_handoff_tool",
    tool_description_override="Custom description",
)
```

## 任务转移输入

在某些情况下，你希望LLM在调用任务转移时提供一些数据。例如，假设要将任务转移给一个“升级处理智能体”。你可能希望模型提供原因，以便将其记录下来。

```python
from pydantic import BaseModel

from agents import Agent, handoff, RunContextWrapper

class EscalationData(BaseModel):
    reason: str

async def on_handoff(ctx: RunContextWrapper[None], input_data: EscalationData):
    print(f"Escalation agent called with reason: {input_data.reason}")

agent = Agent(name="Escalation agent")

handoff_obj = handoff(
    agent=agent,
    on_handoff=on_handoff,
    input_type=EscalationData,
)
```

`input_type` 描述任务转移工具调用本身的参数。SDK会将该架构作为任务转移工具的 `parameters` 提供给模型，在本地验证返回的 JSON，并将解析后的值传递给 `on_handoff`。

它不会替换下一个智能体的主要输入，也不会选择其他目标。[`handoff()`][agents.handoffs.handoff] 辅助函数仍会将任务转移给你封装的特定智能体，而接收任务的智能体仍会看到对话历史记录，除非你使用 [`input_filter`][agents.handoffs.Handoff.input_filter] 或嵌套任务转移历史记录设置对其进行更改。

`input_type` 也独立于 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。请将 `input_type` 用于模型在任务转移时决定的元数据，而不是你在本地已有的应用状态或依赖项。

### `input_type` 的适用场景

当任务转移需要少量由模型生成的元数据（例如 `reason`、`language`、`priority` 或 `summary`）时，请使用 `input_type`。例如，分流智能体可以将任务转移给退款智能体，同时附带 `{ "reason": "duplicate_charge", "priority": "high" }`；在退款智能体接管任务前，`on_handoff` 可以记录或持久化这些元数据。

如果目标不同，请选择其他机制：

-   将现有应用状态和依赖项放入 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。请参阅[上下文指南](context.md)。
-   如果要更改接收任务的智能体所看到的历史记录，请使用 [`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 或 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]。
-   如果存在多个可能的专业智能体，请为每个目标注册一个任务转移。`input_type` 可以向选定的任务转移添加元数据，但不会在不同目标之间进行分派。
-   如果你希望向嵌套的专业智能体提供结构化输入，而不转移对话，请优先使用 [`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool]。请参阅[工具](tools.md#structured-input-for-tool-agents)。

## 输入过滤器

发生任务转移时，新智能体就像接管了对话一样，可以看到此前的完整对话历史记录。如果要更改这一行为，可以设置 [`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个函数，它通过 [`HandoffInputData`][agents.handoffs.HandoffInputData] 接收现有输入，并且必须返回新的 `HandoffInputData`。

[`HandoffInputData`][agents.handoffs.HandoffInputData] 包括：

-   `input_history`：`Runner.run(...)` 启动前的输入历史记录。
-   `pre_handoff_items`：调用任务转移的智能体轮次之前生成的项目。
-   `new_items`：当前轮次中生成的项目，包括任务转移调用和任务转移输出项目。
-   `input_items`：可选项目，用于代替 `new_items` 转发给下一个智能体，让你能够过滤模型输入，同时保持 `new_items` 不变以用于会话历史记录。
-   `run_context`：调用任务转移时处于活动状态的 [`RunContextWrapper`][agents.run_context.RunContextWrapper]。

嵌套任务转移是一项可选择启用的 Beta 功能；在我们对其进行稳定化期间，默认处于禁用状态。启用 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 后，运行器会将可总结的历史记录压缩为按顺序排列的助手摘要片段，同时将无损消息项目保留在其原始位置。每个生成的摘要片段都使用 `<CONVERSATION HISTORY>` 包装器；后续任务转移会先展平之前生成的片段，然后再重新构建有序的对话记录。会话、`RunState` 和 `RunResult.to_input_list()` 会追踪已移入此 SDK 默认历史记录中的确切消息实例，从而避免重复附加这些实例；内容相同但彼此独立的消息仍会保留。你可以通过 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 提供自己的映射函数，以返回下一个智能体所需的确切输入项目列表，而不使用内置分段机制。仅当任务转移和运行均未提供显式 `input_filter` 时，此可选功能才会生效，因此，已经自定义有效负载的现有代码（包括此代码库中的代码示例）无须更改即可保持当前行为。你可以通过向 [`handoff(...)`][agents.handoffs.handoff] 传入 `nest_handoff_history=True` 或 `False`，为单次任务转移覆盖嵌套行为；这会设置 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果只需更改所生成摘要片段的包装器文本，请在运行智能体之前调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（还可选择调用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

如果任务转移和当前 [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] 都定义了过滤器，则对于该次特定的任务转移，任务转移级别的 [`input_filter`][agents.handoffs.Handoff.input_filter] 优先。

!!! note

    任务转移始终在单次运行内进行。输入安全防护措施仍然仅适用于链中的第一个智能体，而输出安全防护措施仅适用于生成最终输出的智能体。如果需要对工作流中的每次自定义函数工具调用执行检查，请使用工具安全防护措施。

有一些常见模式（例如从历史记录中移除所有工具调用），[`agents.extensions.handoff_filters`][] 已为你实现这些模式。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 调用 `FAQ agent` 时，这会自动从历史记录中移除所有工具。

## 推荐提示词

为了确保LLM正确理解任务转移，我们建议在智能体中加入有关任务转移的信息。我们在 [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 中提供了建议的前缀，你也可以调用 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][]，自动向提示词添加建议的数据。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```