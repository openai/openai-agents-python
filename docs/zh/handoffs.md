---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委托给另一个智能体。在不同智能体各自专长不同领域的场景中，这尤其有用。例如，一个客服应用可能有分别处理订单状态、退款、常见问题等任务的智能体。

对 LLM 来说，任务转移以工具的形式呈现。因此，如果要转移给名为 `Refund Agent` 的智能体，相应的工具将被称为 `transfer_to_refund_agent`。

## 创建任务转移

所有智能体都有一个 [`handoffs`][agents.agent.Agent.handoffs] 参数，它可以直接接收一个 `Agent`，或者接收一个用于自定义任务转移的 `Handoff` 对象。

你可以使用 Agents SDK 提供的 [`handoff()`][agents.handoffs.handoff] 函数创建任务转移。该函数允许你指定要转移到的智能体，以及可选的覆盖项和输入过滤器。

### 基本用法

以下展示如何创建一个简单的任务转移：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 你可以直接使用智能体（如 `billing_agent`），也可以使用 `handoff()` 函数。

### 通过 `handoff()` 函数自定义任务转移

[`handoff()`][agents.handoffs.handoff] 函数允许你进行自定义。

- `agent`: 要将任务转移到的智能体。
- `tool_name_override`: 默认使用 `Handoff.default_tool_name()`，其结果为 `transfer_to_<agent_name>`。你可以覆盖该值。
- `tool_description_override`: 覆盖来自 `Handoff.default_tool_description()` 的默认工具描述。
- `on_handoff`: 当任务转移被调用时执行的回调函数。可用于在确认发生任务转移时立即启动某些数据获取等操作。该函数接收智能体上下文，并且可选地接收 LLM 生成的输入。输入数据由 `input_type` 参数控制。
- `input_type`: 任务转移所需的输入类型（可选）。
- `input_filter`: 允许你过滤下一个智能体接收到的输入。详见下文。
- `is_enabled`: 任务转移是否启用。可以是布尔值，或返回布尔值的函数，从而允许你在运行时动态启用或禁用任务转移。

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

在某些情况下，你希望 LLM 在调用任务转移时提供一些数据。例如，想象一个转移到“升级处理智能体（Escalation agent）”的场景。你可能希望提供一个原因，以便记录。

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

## 输入过滤器

当发生任务转移时，就像新智能体接管了对话，并能看到整个先前的对话历史。如果你想更改这一点，可以设置一个 [`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个函数，它通过 [`HandoffInputData`][agents.handoffs.HandoffInputData] 接收现有输入，并且必须返回新的 `HandoffInputData`。

默认情况下，runner 现在会将之前的记录折叠为一条助理摘要消息（见 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]）。该摘要位于一个 `<CONVERSATION HISTORY>` 块内，当同一次运行期间发生多次任务转移时，该块会持续追加新的轮次。你可以通过 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 提供你自己的映射函数，以替换生成的消息，而无需编写完整的 `input_filter`。该默认行为仅在任务转移和运行均未提供显式的 `input_filter` 时适用，因此已自定义负载的现有代码（包括本仓库中的 code examples）将保持当前行为且无需更改。你可以在单次任务转移上通过向 [`handoff(...)`][agents.handoffs.handoff] 传递 `nest_handoff_history=True` 或 `False` 来覆盖嵌套行为，这会设置 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果你只需要更改生成摘要的包装文本，请在运行智能体之前调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及可选的 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

有一些常见模式（例如从历史中移除所有工具调用），已经在 [`agents.extensions.handoff_filters`][] 中为你实现。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 当调用 `FAQ agent` 时，这将自动从历史中移除所有工具调用。

## 推荐提示词

为确保 LLM 能正确理解任务转移，我们建议在你的智能体中包含关于任务转移的信息。我们在 [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 中提供了建议的前缀，或者你可以调用 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] 将推荐的数据自动添加到你的提示词中。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```