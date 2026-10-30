---
search:
  exclude: true
---
# 任务转移

任务转移允许一个智能体将任务委派给另一个智能体。这在不同智能体各自专注于不同领域的场景中特别有用。例如，客户支持应用可能包含多个智能体，分别专门处理订单状态、退款、常见问题等任务。

任务转移以工具的形式呈现给LLM。因此，如果任务要转移给名为`Refund Agent`的智能体，则该工具将命名为`transfer_to_refund_agent`。

## 任务转移的创建 {#creating-a-handoff}

所有智能体都有一个[`handoffs`][agents.agent.Agent.handoffs]参数，它既可以直接接收`Agent`，也可以接收用于自定义任务转移的`Handoff`对象。

如果传入普通的`Agent`实例，其[`handoff_description`][agents.agent.Agent.handoff_description]（设置后）会附加到默认工具描述中。无需编写完整的`handoff()`对象，即可使用它来提示模型应在何时选择该任务转移。

你可以使用Agents SDK提供的[`handoff()`][agents.handoffs.handoff]函数创建任务转移。此函数允许你指定任务将转移到的智能体，以及可选的覆盖项和输入过滤器。

### 基本用法 {#basic-usage}

以下是创建简单任务转移的方法：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 你可以直接使用智能体（如`billing_agent`所示），也可以使用`handoff()`函数。

### 通过`handoff()`函数自定义任务转移 {#customizing-handoffs-via-the-handoff-function}

[`handoff()`][agents.handoffs.handoff]函数允许你自定义以下内容。

-   `agent`：任务将转移到的智能体。
-   `tool_name_override`：默认使用`Handoff.default_tool_name()`函数，其解析结果为`transfer_to_<agent_name>`。你可以覆盖此设置。
-   `tool_description_override`：覆盖来自`Handoff.default_tool_description()`的默认工具描述
-   `on_handoff`：调用任务转移时执行的回调函数。例如，一旦确定正在调用任务转移，就可以立即开始获取某些数据。此函数接收智能体上下文，也可以选择接收LLM生成的输入。输入数据由`input_type`参数控制。
-   `input_type`：任务转移工具调用参数的模式。设置后，解析后的载荷将传递给`on_handoff`。
-   `input_filter`：用于过滤下一个智能体接收的输入。更多信息见下文。
-   `is_enabled`：是否启用任务转移。它可以是布尔值，也可以是返回布尔值的函数，以便在运行时动态启用或禁用任务转移。
-   `nest_handoff_history`：针对每次任务转移的可选覆盖项，用于覆盖RunConfig级别的`nest_handoff_history`设置。如果为`None`，则使用当前运行配置中定义的值。

[`handoff()`][agents.handoffs.handoff]辅助函数始终将控制权转移给你传入的特定`agent`。如果有多个可能的目标，请为每个目标注册一次任务转移，并让模型从中选择。仅当你自己的任务转移代码必须在调用时决定返回哪个智能体时，才使用自定义[`Handoff`][agents.handoffs.Handoff]。

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

## 任务转移输入 {#handoff-inputs}

在某些情况下，你希望LLM在调用任务转移时提供一些数据。例如，假设任务要转移给“升级处理智能体”。你可能希望模型提供原因，以便记录该原因。

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

`input_type`描述任务转移工具调用本身的参数。SDK会将该模式作为任务转移工具的`parameters`提供给模型，在本地验证返回的JSON，并将解析后的值传递给`on_handoff`。

SDK在准备可用的任务转移时会对`is_enabled`求值，此时模型尚未返回任务转移参数，因此它无法对带参数的任务转移中的值进行授权。如果授权取决于解析后的字段，请在`on_handoff`开头、产生任何应用程序副作用之前执行检查。如果授权失败，请抛出异常，而不是返回；`on_handoff`成功返回后，SDK会继续执行转移。工具输入安全防护措施适用于函数工具，而不适用于任务转移。

它不会替换下一个智能体的主要输入，也不会选择其他目标。[`handoff()`][agents.handoffs.handoff]辅助函数仍会将任务转移给你封装的特定智能体，并且接收方智能体仍会看到对话历史记录，除非你使用[`input_filter`][agents.handoffs.Handoff.input_filter]或嵌套任务转移历史记录设置进行更改。

`input_type`也不同于[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。`input_type`应用于模型在任务转移时决定的元数据，而不是本地已有的应用程序状态或依赖项。

### `input_type`的使用时机 {#when-to-use-input_type}

当任务转移需要一小段由模型生成的元数据（例如`reason`、`language`、`priority`或`summary`）时，请使用`input_type`。例如，分流智能体可以通过`{ "reason": "duplicate_charge", "priority": "high" }`将任务转移给退款智能体，而`on_handoff`可以在退款智能体接管之前记录或持久化该元数据。

如果目标不同，请选择其他机制：

-   将现有的应用程序状态和依赖项放入[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]。请参阅[上下文指南](context.md)。
-   如果要更改接收方智能体可见的历史记录，请使用[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]或[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]。
-   如果有多个可能的专业智能体，请为每个目标注册一次任务转移。`input_type`可以向所选的任务转移添加元数据，但不会在不同目标之间进行分派。
-   如果你希望为嵌套的专业智能体提供结构化输入，而不转移对话，请优先使用[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool]。请参阅[工具](tools.md#structured-input-for-tool-agents)。

## 输入过滤器 {#input-filters}

发生任务转移时，就像新智能体接管了对话，并且可以看到此前的完整对话历史记录。如果要更改这一点，可以设置[`input_filter`][agents.handoffs.Handoff.input_filter]。输入过滤器是一个函数，它通过[`HandoffInputData`][agents.handoffs.HandoffInputData]接收现有输入，并且必须返回新的`HandoffInputData`。

[`HandoffInputData`][agents.handoffs.HandoffInputData]包括：

-   `input_history`：`Runner.run(...)`开始之前的输入历史记录。
-   `pre_handoff_items`：调用任务转移的智能体轮次之前生成的项目。
-   `new_items`：当前轮次中生成的项目，包括任务转移调用和任务转移输出项目。
-   `input_items`：可选项目，用于代替`new_items`转发给下一个智能体，让你能够过滤模型输入，同时保持`new_items`不变以用于会话历史记录。
-   `run_context`：调用任务转移时处于活动状态的[`RunContextWrapper`][agents.run_context.RunContextWrapper]。

嵌套任务转移历史记录是一项可选择启用的测试版功能，在我们对其进行稳定化期间，默认处于禁用状态。启用[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]后，运行器会将可摘要的历史记录压缩为按顺序排列的助手摘要片段，同时将无损消息项保留在其原始位置。每个生成的摘要片段都使用`<CONVERSATION HISTORY>`包装器，后续任务转移会先展平此前生成的片段，再重新构建有序的对话记录。会话、`RunState`和`RunResult.to_input_list()`会追踪移入此SDK默认历史记录中的确切消息实例，以免重复附加这些实例；彼此独立但内容相同的消息仍会保留。你可以通过[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]提供自己的映射函数，返回下一个智能体所需的确切输入项目列表，而不使用内置分段机制。仅当任务转移的`input_filter`和当前运行的`RunConfig.handoff_input_filter`均未设置时，这项可选功能才适用，因此已经自定义载荷的现有代码（包括此代码仓库中的代码示例）无需更改即可保持当前行为。你可以向[`handoff(...)`][agents.handoffs.handoff]传入`nest_handoff_history=True`或`False`，为单次任务转移覆盖嵌套行为；这会设置[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]。如果只需更改生成的摘要片段所用的包装器文本，请在运行智能体之前调用[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]。如果后续运行需要恢复默认包装器，请在该运行之前调用[`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]。

嵌套任务转移历史记录会改变对话记录的表示方式，但不会隐去敏感数据。即使相应的结构化工具项不再单独转发，工具调用参数和工具输出仍可能保留在生成的助手摘要中。请将接收方智能体及其模型提供商视为转发历史记录的接收方。

对于客户端管理的历史记录，请使用显式的[`input_filter`][agents.handoffs.Handoff.input_filter]或[`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]来选择或隐去接收方智能体可以看到的内容。如果自定义过滤器还调用`nest_handoff_history`，请在调用之前清理`input_history`、`pre_handoff_items`和`new_items`。该辅助函数会根据这三个字段构建嵌套历史记录，并忽略任何现有的`input_items`覆盖项。因此，仅过滤`input_items`仍可能使已排除的工具内容出现在生成的摘要中。

如果过滤器必须为会话历史记录保留原始`new_items`，则可以改为调用`nest_handoff_history`，并在返回嵌套结果之前清理所返回的`input_history`。嵌套完成后，仅清空或替换`input_items`并不会移除已包含在`input_history`中的内容。

服务器管理的对话（`conversation_id`、`previous_response_id`或`auto_previous_response_id`）不支持任务转移输入过滤器；如果接收方智能体不得继承该服务器管理的历史记录，请通过明确选择的输入启动单独的运行。请勿在该单独运行中重复使用原始`conversation_id`或`previous_response_id`。

如果任务转移和当前的[`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]都定义了过滤器，则对于该次特定任务转移，每次任务转移专属的[`input_filter`][agents.handoffs.Handoff.input_filter]具有更高优先级。

!!! note

    任务转移始终在单次运行内进行。输入安全防护措施仍仅适用于链中的第一个智能体，而输出安全防护措施仅适用于生成最终输出的智能体。如果需要检查工作流中的每次自定义函数工具调用，请使用工具安全防护措施。

一些常见模式（例如从历史记录中移除所有工具调用）已在[`agents.extensions.handoff_filters`][]中为你实现。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools,  # (1)!
)
```

1. 调用`FAQ agent`时，这会自动从历史记录中移除所有与工具相关的项目。

`remove_all_tools`会移除结构化工具项。它不会隐去已复制到普通消息或嵌套历史记录摘要中的工具参数或结果。需要时，请使用自定义输入过滤器移除或隐去这些消息内容。

## 推荐提示词 {#recommended-prompts}

为确保LLM正确理解任务转移，我们建议在智能体中包含有关任务转移的信息。我们在[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][]中提供了建议的前缀，你也可以调用[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][]，自动将推荐的数据添加到提示词中。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```