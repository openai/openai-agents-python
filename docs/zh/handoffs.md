---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委派给另一个智能体。这在不同智能体专精于不同领域的场景中特别有用。例如，一个客户支持应用可能有多个智能体，分别专门处理订单状态、退款、常见问题等任务。

对 LLM 而言，任务转移表示为工具。因此，如果有一个任务转移到名为 `Refund Agent` 的智能体，对应的工具会被命名为 `transfer_to_refund_agent`。

## 任务转移的创建

所有智能体都有一个 [`handoffs`][agents.agent.Agent.handoffs] 参数，它既可以直接接收一个 `Agent`，也可以接收一个用于自定义任务转移的 `Handoff` 对象。

如果传入普通的 `Agent` 实例，它们的 [`handoff_description`][agents.agent.Agent.handoff_description]（如果已设置）会附加到默认工具描述之后。可用它来提示模型何时应选择该任务转移，而无需编写完整的 `handoff()` 对象。

你可以使用 Agents SDK 提供的 [`handoff()`][agents.handoffs.handoff] 函数来创建任务转移。该函数允许你指定要转移到的智能体，并可选择指定覆盖项和输入过滤器。

### 基本用法

下面是创建一个简单任务转移的方法：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 你可以直接使用智能体（如 `billing_agent`），也可以使用 `handoff()` 函数。

### 通过 `handoff()` 函数进行的任务转移自定义

[`handoff()`][agents.handoffs.handoff] 函数允许你自定义相关内容。

-   `agent`: 这是任务将被转移到的智能体。
-   `tool_name_override`: 默认情况下会使用 `Handoff.default_tool_name()` 函数，它会解析为 `transfer_to_<agent_name>`。你可以覆盖它。
-   `tool_description_override`: 覆盖来自 `Handoff.default_tool_description()` 的默认工具描述
-   `on_handoff`: 在任务转移被调用时执行的回调函数。这对于在确认任务转移被调用后立即启动某些数据获取等操作很有用。该函数会接收智能体上下文，也可以选择接收 LLM 生成的输入。输入数据由 `input_type` 参数控制。
-   `input_type`: 任务转移工具调用参数的 schema。设置后，解析后的负载会传递给 `on_handoff`。
-   `input_filter`: 它允许你过滤下一个智能体接收到的输入。更多信息见下文。
-   `is_enabled`: 任务转移是否启用。它可以是一个布尔值，也可以是返回布尔值的函数，从而允许你在运行时动态启用或禁用任务转移。
-   `nest_handoff_history`: 对 RunConfig 级别 `nest_handoff_history` 设置的可选单次调用覆盖。如果为 `None`，则改用当前活动运行配置中定义的值。

[`handoff()`][agents.handoffs.handoff] 辅助函数始终会将控制权转移给你传入的特定 `agent`。如果有多个可能的目标，请为每个目标注册一个任务转移，并让模型在它们之间选择。仅当你自己的任务转移代码必须在调用时决定返回哪个智能体时，才使用自定义 [`Handoff`][agents.handoffs.Handoff]。

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

在某些情况下，你希望 LLM 在调用任务转移时提供一些数据。例如，设想有一个转移到“Escalation agent”的任务转移。你可能希望模型提供一个原因，以便你记录它。

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

`input_type` 描述任务转移工具调用本身的参数。SDK 会将该 schema 作为任务转移工具的 `parameters` 暴露给模型，在本地验证返回的 JSON，并将解析后的值传递给 `on_handoff`。

它不会替换下一个智能体的主输入，也不会选择不同的目标。[`handoff()`][agents.handoffs.handoff] 辅助函数仍然会转移到你包装的特定智能体，并且接收方智能体仍会看到对话历史，除非你通过 [`input_filter`][agents.handoffs.Handoff.input_filter] 或嵌套任务转移历史设置对其进行更改。

`input_type` 也与 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] 分离。请将 `input_type` 用于模型在任务转移时决定的元数据，而不是用于你本地已有的应用状态或依赖项。

### `input_type` 的使用场景

当任务转移需要一小段模型生成的元数据时，请使用 `input_type`，例如 `reason`、`language`、`priority` 或 `summary`。例如，分诊智能体可以通过 `{ "reason": "duplicate_charge", "priority": "high" }` 转移给退款智能体，`on_handoff` 可以在退款智能体接管之前记录或持久化这些元数据。

当目标不同时，请选择其他机制：

-   将现有的应用状态和依赖项放入 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。参见[上下文指南](context.md)。
-   如果你想更改接收方智能体看到的历史，请使用 [`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 或 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]。
-   如果有多个可能的专家智能体，请为每个目标注册一个任务转移。`input_type` 可以向所选任务转移添加元数据，但不会在不同目标之间进行分派。
-   如果你希望为嵌套专家提供结构化输入而不转移对话，请优先使用 [`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool]。参见[工具](tools.md#structured-input-for-tool-agents)。

## 输入过滤器

当发生任务转移时，就像新的智能体接管了对话，并且能够看到之前的完整对话历史。如果你想更改这一点，可以设置 [`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个函数，它通过 [`HandoffInputData`][agents.handoffs.HandoffInputData] 接收现有输入，并且必须返回一个新的 `HandoffInputData`。

[`HandoffInputData`][agents.handoffs.HandoffInputData] 包括：

-   `input_history`: `Runner.run(...)` 启动前的输入历史。
-   `pre_handoff_items`: 在调用任务转移的智能体轮次之前生成的项目。
-   `new_items`: 当前轮次期间生成的项目，包括任务转移调用和任务转移输出项目。
-   `input_items`: 可选项目，用于转发给下一个智能体以替代 `new_items`，允许你过滤模型输入，同时保持 `new_items` 完整以用于会话历史。
-   `run_context`: 调用任务转移时处于活动状态的 [`RunContextWrapper`][agents.run_context.RunContextWrapper]。

嵌套任务转移作为可选择启用的 beta 功能提供，在我们稳定它们之前默认处于禁用状态。当你启用 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 时，运行器会将先前的转录折叠为一条助手摘要消息，并将其包装在 `<CONVERSATION HISTORY>` 块中；当同一次运行中发生多次任务转移时，该块会持续追加新的轮次。你可以通过 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 提供自己的映射函数，以替换生成的消息，而无需编写完整的 `input_filter`。只有当任务转移和运行都没有提供显式 `input_filter` 时，该选择启用项才会生效，因此已经自定义负载的现有代码（包括此仓库中的代码示例）会保持当前行为而无需更改。你可以通过向 [`handoff(...)`][agents.handoffs.handoff] 传递 `nest_handoff_history=True` 或 `False` 来覆盖单个任务转移的嵌套行为，这会设置 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果你只需要更改生成摘要的包装文本，请在运行智能体之前调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并可选择调用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

如果任务转移和活动的 [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] 都定义了过滤器，则对于该特定任务转移，逐任务转移的 [`input_filter`][agents.handoffs.Handoff.input_filter] 优先。

!!! note

    任务转移会保持在单次运行内。输入安全防护措施仍然只应用于链中的第一个智能体，输出安全防护措施只应用于生成最终输出的智能体。当你需要围绕工作流中每个自定义函数工具调用进行检查时，请使用工具安全防护措施。

有一些常见模式（例如从历史中移除所有工具调用）已经在 [`agents.extensions.handoff_filters`][] 中为你实现。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 当调用 `FAQ agent` 时，这会自动从历史中移除所有工具。

## 推荐提示词

为确保 LLM 正确理解任务转移，我们建议在你的智能体中包含有关任务转移的信息。我们在 [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 中提供了建议的前缀，或者你可以调用 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] 来自动将推荐数据添加到你的提示词中。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```