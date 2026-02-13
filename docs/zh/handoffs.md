---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委派给另一个智能体。这在不同智能体分别专精于不同领域的场景中特别有用。例如，一个客户支持应用可能包含多个智能体，分别专门处理订单状态、退款、常见问题解答等任务。

任务转移会以工具的形式呈现给 LLM。因此，如果要任务转移到名为 `Refund Agent` 的智能体，该工具会被命名为 `transfer_to_refund_agent`。

## 创建任务转移

所有智能体都有一个 [`handoffs`][agents.agent.Agent.handoffs] 参数，它既可以直接接收一个 `Agent`，也可以接收一个用于自定义任务转移的 `Handoff` 对象。

如果你传入的是普通的 `Agent` 实例，它们的 [`handoff_description`][agents.agent.Agent.handoff_description]（设置后）会被追加到默认的工具描述中。你可以用它来提示模型在何时应选择该任务转移，而无需编写完整的 `handoff()` 对象。

你可以使用 Agents SDK 提供的 [`handoff()`][agents.handoffs.handoff] 函数来创建任务转移。该函数允许你指定要转移到的智能体，并提供可选的覆写项与输入过滤器。

### 基本用法

下面展示如何创建一个简单的任务转移：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 你可以直接使用智能体（如 `billing_agent`），也可以使用 `handoff()` 函数。

### 通过 `handoff()` 函数自定义任务转移

[`handoff()`][agents.handoffs.handoff] 函数允许你自定义相关内容。

-   `agent`：要将任务转移给的智能体。
-   `tool_name_override`：默认使用 `Handoff.default_tool_name()` 函数，解析为 `transfer_to_<agent_name>`。你可以覆写它。
-   `tool_description_override`：覆写 `Handoff.default_tool_description()` 的默认工具描述。
-   `on_handoff`：当任务转移被调用时执行的回调函数。这对在你确定将发生任务转移时立即触发数据拉取等操作很有用。该函数会接收智能体上下文，并且也可以选择接收由 LLM 生成的输入。输入数据由 `input_type` 参数控制。
-   `input_type`：任务转移期望的输入类型（可选）。
-   `input_filter`：用于过滤下一个智能体接收到的输入。更多内容见下文。
-   `is_enabled`：任务转移是否启用。可以是布尔值，也可以是返回布尔值的函数，从而支持在运行时动态启用或禁用任务转移。

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

在某些情况下，你希望 LLM 在调用任务转移时提供一些数据。例如，设想有一个转移到“升级处理智能体”的任务转移。你可能希望提供一个原因，以便记录日志。

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

当发生任务转移时，就像新的智能体接管了对话，并能看到此前的全部对话历史。如果你想改变这一点，可以设置一个 [`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个函数，它通过 [`HandoffInputData`][agents.handoffs.HandoffInputData] 接收现有输入，并必须返回一个新的 `HandoffInputData`。

嵌套任务转移以可选加入（opt-in）的 beta 形式提供；在我们稳定它们期间，默认是禁用的。当你启用 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 时，runner 会将此前的对话记录折叠为一条 assistant 总结消息，并将其包裹在一个 `<CONVERSATION HISTORY>` 块中；当同一次运行期间发生多次任务转移时，该块会持续追加新的轮次。你可以通过 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 提供自己的映射函数，以在不编写完整 `input_filter` 的情况下替换生成的消息。该 opt-in 仅在任务转移和运行都未提供显式 `input_filter` 时生效，因此现有已经自定义 payload 的代码（包括本仓库中的代码示例）无需修改即可保持当前行为。你可以通过在 [`handoff(...)`][agents.handoffs.handoff] 中传入 `nest_handoff_history=True` 或 `False` 来为单个任务转移覆写嵌套行为，这会设置 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果你只需要修改生成总结的包装文本，请在运行智能体之前调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并可选调用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

有一些常见模式（例如从历史记录中移除所有工具调用），我们已在 [`agents.extensions.handoff_filters`][] 中为你实现。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 当调用 `FAQ agent` 时，这会自动从历史记录中移除所有工具。

## 推荐提示词

为确保 LLM 正确理解任务转移，我们建议在你的智能体中包含有关任务转移的信息。我们在 [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 中提供了建议的前缀；或者你可以调用 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] 来自动将推荐数据添加到你的提示词中。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```