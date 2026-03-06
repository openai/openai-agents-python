---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委派给另一个智能体。这在不同智能体专注于不同领域的场景中特别有用。例如，一个客户支持应用可能会有多个智能体，分别专门处理订单状态、退款、常见问题等任务。

任务转移会作为工具呈现给 LLM。因此，如果有一个转移目标是名为 `Refund Agent` 的智能体，那么该工具名称会是 `transfer_to_refund_agent`。

## 创建任务转移

所有智能体都有一个 [`handoffs`][agents.agent.Agent.handoffs] 参数，它既可以直接接收一个 `Agent`，也可以接收一个用于自定义任务转移的 `Handoff` 对象。

如果你传入普通的 `Agent` 实例，它们的 [`handoff_description`][agents.agent.Agent.handoff_description]（设置时）会附加到默认工具描述中。你可以用它提示模型何时应选择该任务转移，而无需编写完整的 `handoff()` 对象。

你可以使用 Agents SDK 提供的 [`handoff()`][agents.handoffs.handoff] 函数创建任务转移。该函数允许你指定要转移到的智能体，以及可选的覆盖项和输入过滤器。

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

### 通过 `handoff()` 函数自定义任务转移

[`handoff()`][agents.handoffs.handoff] 函数允许你自定义配置。

-   `agent`：这是要将任务转移到的智能体。
-   `tool_name_override`：默认使用 `Handoff.default_tool_name()` 函数，结果为 `transfer_to_<agent_name>`。你可以覆盖它。
-   `tool_description_override`：覆盖 `Handoff.default_tool_description()` 的默认工具描述
-   `on_handoff`：在任务转移被调用时执行的回调函数。这对于在你确认任务转移将被调用后立即触发数据获取等场景很有用。该函数会接收智能体上下文，并且也可以选择接收由 LLM 生成的输入。输入数据由 `input_type` 参数控制。
-   `input_type`：任务转移工具调用参数的 schema。设置后，解析后的负载会传递给 `on_handoff`。
-   `input_filter`：允许你过滤下一个智能体接收到的输入。详见下文。
-   `is_enabled`：任务转移是否启用。可以是布尔值，也可以是返回布尔值的函数，从而允许你在运行时动态启用或禁用任务转移。
-   `nest_handoff_history`：对 RunConfig 级别 `nest_handoff_history` 设置的可选单次调用覆盖项。如果为 `None`，则改用当前运行配置中定义的值。

[`handoff()`][agents.handoffs.handoff] 辅助函数始终将控制权转移到你传入的特定 `agent`。如果你有多个可能的目标，请为每个目标注册一个任务转移，并让模型在它们之间选择。仅当你自己的任务转移代码必须在调用时决定返回哪个智能体时，才使用自定义 [`Handoff`][agents.handoffs.Handoff]。

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

在某些情况下，你会希望 LLM 在调用任务转移时提供一些数据。例如，设想有一个到“升级处理智能体”的任务转移。你可能希望提供原因，以便记录日志。

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

`input_type` 描述的是任务转移工具调用本身的参数。SDK 会将该 schema 作为任务转移工具的 `parameters` 暴露给模型，在本地校验返回的 JSON，并将解析后的值传递给 `on_handoff`。

它不会替代下一个智能体的主输入，也不会选择不同的目标。[`handoff()`][agents.handoffs.handoff] 辅助函数仍会转移到你封装的特定智能体，接收方智能体仍会看到对话历史，除非你通过 [`input_filter`][agents.handoffs.Handoff.input_filter] 或嵌套任务转移历史设置进行更改。

`input_type` 也独立于 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。`input_type` 适用于模型在任务转移时决定的元数据，而不是你本地已存在的应用状态或依赖项。

### 何时使用 `input_type`

当任务转移需要一小段由模型生成的元数据（如 `reason`、`language`、`priority` 或 `summary`）时，使用 `input_type`。例如，分流智能体可以将任务转移给退款智能体并附带 `{ "reason": "duplicate_charge", "priority": "high" }`，而 `on_handoff` 可以在退款智能体接管前记录或持久化该元数据。

当目标不同，请选择其他机制：

-   将现有应用状态和依赖项放入 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。参见[上下文指南](context.md)。
-   如果你想更改接收方智能体能看到的历史，使用 [`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 或 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]。
-   如果存在多个可能的专家目标，为每个目标注册一个任务转移。`input_type` 可以为已选任务转移添加元数据，但不会在目标之间分发。
-   如果你想为嵌套专家提供 structured outputs 输入而不转移对话，优先使用 [`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool]。参见 [tools](tools.md#structured-input-for-tool-agents)。

## 输入过滤器

当发生任务转移时，就好像新智能体接管了对话，并能看到此前完整的对话历史。如果你想改变这一点，可以设置 [`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个函数，它通过 [`HandoffInputData`][agents.handoffs.HandoffInputData] 接收现有输入，并且必须返回一个新的 `HandoffInputData`。

[`HandoffInputData`][agents.handoffs.HandoffInputData] 包含：

-   `input_history`：`Runner.run(...)` 开始前的输入历史。
-   `pre_handoff_items`：调用任务转移的智能体轮次之前生成的条目。
-   `new_items`：当前轮次中生成的条目，包括任务转移调用和任务转移输出条目。
-   `input_items`：可选项；可转发给下一个智能体以替代 `new_items`，从而在保留用于会话历史的 `new_items` 不变的同时过滤模型输入。
-   `run_context`：调用任务转移时处于激活状态的 [`RunContextWrapper`][agents.run_context.RunContextWrapper]。

嵌套任务转移作为可选启用的 beta 功能提供，默认关闭，直到我们将其稳定化。启用 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 后，runner 会将先前的对话记录折叠为一条 assistant 摘要消息，并将其包装在 `<CONVERSATION HISTORY>` 块中；当同一次运行中发生多次任务转移时，该块会持续追加新轮次。你可以通过 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 提供自己的映射函数来替换自动生成的消息，而无需编写完整的 `input_filter`。仅当任务转移和运行都未提供显式 `input_filter` 时，此可选启用才会生效，因此已自定义负载的现有代码（包括本仓库中的代码示例）无需变更即可保持当前行为。你可以在 [`handoff(...)`][agents.handoffs.handoff] 中传入 `nest_handoff_history=True` 或 `False` 来覆盖单次任务转移的嵌套行为，这会设置 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果你只需要修改生成摘要的包装文本，请在运行智能体前调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及可选的 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

如果任务转移和当前激活的 [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] 都定义了过滤器，则该特定任务转移的每任务转移 [`input_filter`][agents.handoffs.Handoff.input_filter] 优先。

!!! note

    任务转移会保持在单次运行内。输入安全防护措施仍仅适用于链路中的第一个智能体，输出安全防护措施仅适用于产生最终输出的智能体。当你需要在工作流中每次自定义工具调用周围进行检查时，请使用工具安全防护措施。

有一些常见模式（例如从历史中移除所有工具调用）已在 [`agents.extensions.handoff_filters`][] 中为你实现。

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

为了确保 LLM 正确理解任务转移，我们建议在你的智能体中包含任务转移相关信息。我们在 [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 中提供了建议前缀，或者你可以调用 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][]，将推荐内容自动添加到你的提示词中。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```