---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委派给另一个智能体。这在不同智能体专长于不同领域的场景中特别有用。例如，客服应用可能有多个智能体，分别专门处理订单状态、退款、FAQ 等任务。

任务转移会作为工具呈现给 LLM。因此，如果存在一个转移到名为 `Refund Agent` 的智能体的任务转移，该工具会被称为 `transfer_to_refund_agent`。

## 任务转移的创建

所有智能体都有一个 [`handoffs`][agents.agent.Agent.handoffs] 参数，它既可以直接接收一个 `Agent`，也可以接收一个用于自定义任务转移的 `Handoff` 对象。

如果传入普通的 `Agent` 实例，它们的 [`handoff_description`][agents.agent.Agent.handoff_description]（如果已设置）会追加到默认工具描述中。可以使用它来提示模型应在何时选择该任务转移，而无需编写完整的 `handoff()` 对象。

你可以使用 Agents SDK 提供的 [`handoff()`][agents.handoffs.handoff] 函数创建任务转移。此函数允许你指定要转移到的智能体，并提供可选的覆盖项和输入过滤器。

### 基本用法

下面是创建简单任务转移的方法：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 你可以直接使用智能体（如 `billing_agent`），也可以使用 `handoff()` 函数。

### 基于 `handoff()` 函数的任务转移自定义

[`handoff()`][agents.handoffs.handoff] 函数允许你自定义相关内容。

-   `agent`: 这是任务将被转移到的智能体。
-   `tool_name_override`: 默认使用 `Handoff.default_tool_name()` 函数，其结果为 `transfer_to_<agent_name>`。你可以覆盖此项。
-   `tool_description_override`: 覆盖来自 `Handoff.default_tool_description()` 的默认工具描述
-   `on_handoff`: 在任务转移被调用时执行的回调函数。当你知道任务转移正在被调用时，需要立即启动某些数据获取等操作，这会很有用。此函数接收智能体上下文，并且也可以选择接收由 LLM 生成的输入。输入数据由 `input_type` 参数控制。
-   `input_type`: 任务转移工具调用参数的模式。设置后，解析后的载荷会传递给 `on_handoff`。
-   `input_filter`: 可用于过滤下一个智能体接收的输入。更多信息见下文。
-   `is_enabled`: 任务转移是否启用。它可以是布尔值，或返回布尔值的函数，从而允许你在运行时动态启用或禁用该任务转移。
-   `nest_handoff_history`: 用于覆盖 RunConfig 级别 `nest_handoff_history` 设置的可选单次调用配置。如果为 `None`，则改用当前生效运行配置中定义的值。

[`handoff()`][agents.handoffs.handoff] 辅助函数始终将控制权转移给你传入的特定 `agent`。如果有多个可能的目标，请为每个目标注册一个任务转移，并让模型在其中选择。只有当你自己的任务转移代码必须在调用时决定返回哪个智能体时，才使用自定义 [`Handoff`][agents.handoffs.Handoff]。

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

在某些情况下，你希望 LLM 在调用任务转移时提供一些数据。例如，设想一个转移到“升级智能体”的任务转移。你可能希望提供一个原因，以便记录它。

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

`input_type` 描述任务转移工具调用本身的参数。SDK 会将该模式作为任务转移工具的 `parameters` 暴露给模型，在本地验证返回的 JSON，并将解析后的值传递给 `on_handoff`。

它不会替换下一个智能体的主输入，也不会选择不同的目标。[`handoff()`][agents.handoffs.handoff] 辅助函数仍会转移到你包装的特定智能体，并且接收方智能体仍会看到对话历史，除非你使用 [`input_filter`][agents.handoffs.Handoff.input_filter] 或嵌套任务转移历史设置来更改它。

`input_type` 也不同于 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。请将 `input_type` 用于模型在任务转移时决定的元数据，而不是用于你在本地已有的应用状态或依赖项。

### `input_type` 的使用场景

当任务转移需要一小段由模型生成的元数据（例如 `reason`、`language`、`priority` 或 `summary`）时，使用 `input_type`。例如，分流智能体可以使用 `{ "reason": "duplicate_charge", "priority": "high" }` 转移给退款智能体，而 `on_handoff` 可以在退款智能体接管之前记录或持久化该元数据。

当目标不同时，请选择其他机制：

-   将已有的应用状态和依赖项放入 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。请参阅[上下文指南](context.md)。
-   如果你想更改接收方智能体看到的历史，请使用 [`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 或 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]。
-   如果存在多个可能的专业智能体，请为每个目标注册一个任务转移。`input_type` 可以向所选任务转移添加元数据，但不会在各目标之间进行分派。
-   如果你希望为嵌套的专业智能体提供结构化输入，而不转移对话，请优先使用 [`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool]。请参阅[工具](tools.md#structured-input-for-tool-agents)。

## 输入过滤器

当发生任务转移时，就像新的智能体接管对话一样，它可以看到此前完整的对话历史。如果你想更改这一点，可以设置 [`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个通过 [`HandoffInputData`][agents.handoffs.HandoffInputData] 接收现有输入的函数，并且必须返回新的 `HandoffInputData`。

[`HandoffInputData`][agents.handoffs.HandoffInputData] 包括：

-   `input_history`: `Runner.run(...)` 启动之前的输入历史。
-   `pre_handoff_items`: 在调用任务转移的智能体轮次之前生成的项。
-   `new_items`: 当前轮次期间生成的项，包括任务转移调用和任务转移输出项。
-   `input_items`: 可选项，用于转发给下一个智能体以替代 `new_items`，使你能够过滤模型输入，同时保持 `new_items` 在会话历史中不变。
-   `run_context`: 任务转移被调用时处于活动状态的 [`RunContextWrapper`][agents.run_context.RunContextWrapper]。

嵌套任务转移以可选择启用的 beta 形式提供，在我们稳定该功能期间默认禁用。当你启用 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 时，运行器会将先前的对话记录折叠为一条助手摘要消息，并将其包装在 `<CONVERSATION HISTORY>` 块中；当同一次运行中发生多次任务转移时，该块会持续追加新的轮次。你可以通过 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 提供自己的映射函数来替换生成的消息，而无需编写完整的 `input_filter`。只有在任务转移和运行都没有提供显式 `input_filter` 时，此可选启用机制才会生效，因此已经自定义载荷的现有代码（包括本仓库中的代码示例）会保持当前行为，无需更改。你可以通过向 [`handoff(...)`][agents.handoffs.handoff] 传入 `nest_handoff_history=True` 或 `False` 来覆盖单个任务转移的嵌套行为，这会设置 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果你只需要更改生成摘要的包装文本，请在运行智能体之前调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（也可选择调用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

如果任务转移和当前生效的 [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] 都定义了过滤器，则针对该特定任务转移，任务转移级别的 [`input_filter`][agents.handoffs.Handoff.input_filter] 优先。

!!! note

    任务转移会停留在单次运行内。输入安全防护措施仍然只应用于链路中的第一个智能体，输出安全防护措施也只应用于生成最终输出的智能体。当你需要围绕工作流中的每个自定义函数工具调用进行检查时，请使用工具安全防护措施。

有一些常见模式（例如从历史中移除所有工具调用），已在 [`agents.extensions.handoff_filters`][] 中为你实现

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 这会在调用 `FAQ agent` 时自动从历史中移除所有工具。

## 推荐提示词

为了确保 LLMs 能够正确理解任务转移，我们建议在你的智能体中包含有关任务转移的信息。我们在 [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 中提供了建议前缀，或者你可以调用 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] 自动向你的提示词添加推荐数据。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```