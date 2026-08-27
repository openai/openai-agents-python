---
search:
  exclude: true
---
# 流式传输

流式传输允许你订阅智能体运行过程中的更新。这对于向最终用户展示进度更新和部分响应非常有用。

要进行流式传输，你可以调用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，它会返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。调用 `result.stream_events()` 可获得由 [`StreamEvent`][agents.stream_events.StreamEvent] 对象组成的异步流，具体说明如下。

持续使用 `result.stream_events()` 进行消费，直到异步迭代器结束。只有迭代器结束后，流式运行才算完成；会话持久化、审批记录维护或历史压缩等后处理可能会在最后一个可见 token 到达后才完成。当循环退出时，`result.is_complete` 会反映最终的运行状态。

## 原始响应事件 {#raw-response-events}

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 对象封装了直接从 LLM 传递的原始事件。每个对象的 `data` 字段都包含一个 OpenAI Responses API 事件，其类型可能是 `response.created` 或 `response.output_text.delta`。如果你希望响应消息一经生成就立即以流式方式发送给用户，这些事件会非常有用。

计算机工具的原始事件与已存储结果保持相同的预览版与正式发布版之分。预览版流程会流式传输带有一个 `action` 的 `computer_call` 条目，而 `gpt-5.5` 可以流式传输带有批量 `actions[]` 的 `computer_call` 条目。更高层级的 [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 接口不会为此添加计算机工具专用的特殊事件名称：这两种结构仍然都以 `tool_called` 的形式呈现，而截图结果会以封装 `computer_call_output` 条目的 `tool_output` 形式返回。

例如，以下代码会逐 token 输出 LLM 生成的文本。

```python
import asyncio
from openai.types.responses import ResponseTextDeltaEvent
from agents import Agent, Runner

async def main():
    agent = Agent(
        name="Joker",
        instructions="You are a helpful assistant.",
    )

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
```

## 流式传输与审批 {#streaming-and-approvals}

流式传输与因工具审批而暂停的运行兼容。如果工具需要审批，`result.stream_events()` 会结束，待处理的审批则会在 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中公开。使用 `result.to_state()` 将结果转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝中断，然后使用 `Runner.run_streamed(...)` 恢复运行。

```python
result = Runner.run_streamed(agent, "Delete temporary files if they are no longer needed.")
async for _event in result.stream_events():
    pass

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = Runner.run_streamed(agent, state)
    async for _event in result.stream_events():
        pass
```

有关完整的暂停和恢复操作流程，请参阅[人在回路指南](human_in_the_loop.md)。

## 当前轮次结束后的流式传输取消 {#cancel-streaming-after-the-current-turn}

如果需要中途停止流式运行，请调用 [`result.cancel()`][agents.result.RunResultStreaming.cancel]。默认情况下，这会立即停止运行。要让当前轮次正常完成后再停止，请改为调用 `result.cancel(mode="after_turn")`。

只有 `result.stream_events()` 结束后，流式运行才算完成。在最后一个可见 token 到达后，SDK 可能仍在持久化会话条目、确定最终审批状态或压缩历史记录。

如果你正从 [`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] 手动继续，并且 `cancel(mode="after_turn")` 在某个工具轮次后停止，请使用该规范化输入重新运行 `result.last_agent`，以继续尚未完成的现有用户轮次，而不是立即追加一个新的用户轮次。

-   如果在该未完成的运行恢复前收到了新的用户输入，请使用 `result.to_state()` 转换已消费完毕的结果，调用 [`state.add_input(...)`][agents.run_state.RunState.add_input]，然后从该状态恢复运行。运行器会在下一次模型调用前立即接纳暂存的输入；请参阅[恢复前添加输入](results.md#add-input-before-resuming)。
-   如果流式运行因工具审批而停止，请勿将其视为新轮次。应先将流消费完毕，检查 `result.interruptions`，然后改为从 `result.to_state()` 恢复运行。
-   使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 自定义如何在下一次模型调用前合并检索到的会话历史与新的用户输入。如果你在此处重写新轮次条目，则重写后的版本会作为该轮次的持久化内容。

## 运行条目事件与智能体事件 {#run-item-events-and-agent-events}

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 是更高层级的事件。它们会在条目完全生成后通知你。这样，你便可以按“消息已生成”“工具已运行”等粒度推送进度更新，而不是按每个 token 推送。同样，[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] 会在当前智能体发生变化时向你提供更新（例如，因任务转移而发生变化）。

### 运行条目事件名称 {#run-item-event-names}

`RunItemStreamEvent.name` 使用一组固定的语义事件名称：

-   `message_output_created`
-   `handoff_requested`
-   `handoff_occured`
-   `tool_called`
-   `tool_search_called`
-   `tool_search_output_created`
-   `tool_output`
-   `reasoning_item_created`
-   `mcp_approval_requested`
-   `mcp_approval_response`
-   `mcp_list_tools`

为保持向后兼容，`handoff_occured` 特意保留了拼写错误。

任务转移调用只会以 `handoff_requested` 的形式发出，不会同时以 `tool_called` 的形式发出。同一轮次中的普通函数工具调用仍会发出 `tool_called`。

使用托管工具检索时，当模型发出工具检索请求，会发出 `tool_search_called`；当 Responses API 返回已加载的子集时，会发出 `tool_search_output_created`。

使用程序化工具调用时，系统会为生成的 `program` 以及由程序拥有的普通子工具调用发出 `tool_called`。系统会为子工具输出以及与生成的 `program` 相匹配的 `program_output` 发出 `tool_output`。由程序拥有的托管 MCP `mcp_approval_request` 和 `mcp_list_tools` 条目属于例外：它们会分别以 `mcp_approval_requested` 和 `mcp_list_tools` 的形式发出，并分别封装 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem] 和 [`MCPListToolsItem`][agents.items.MCPListToolsItem]。检查原始条目的 `type` 以区分其余条目；由程序拥有的子调用还带有一个类型为 `program` 的 `caller`，其调用方 ID 用于标识父程序。

例如，以下代码会忽略原始事件，并以流式方式向用户发送更新。

```python
import asyncio
import random
from agents import Agent, ItemHelpers, Runner
from agents.decorators import tool

@tool
def how_many_jokes() -> int:
    return random.randint(1, 10)


async def main():
    agent = Agent(
        name="Joker",
        instructions="First call the `how_many_jokes` tool, then tell that many jokes.",
        tools=[how_many_jokes],
    )

    result = Runner.run_streamed(
        agent,
        input="Hello",
    )
    print("=== Run starting ===")

    async for event in result.stream_events():
        # We'll ignore the raw responses event deltas
        if event.type == "raw_response_event":
            continue
        # When the agent updates, print that
        elif event.type == "agent_updated_stream_event":
            print(f"Agent updated: {event.new_agent.name}")
            continue
        # When items are generated, print them
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print("-- Tool was called")
            elif event.item.type == "tool_call_output_item":
                print(f"-- Tool output: {event.item.output}")
            elif event.item.type == "message_output_item":
                print(f"-- Message output:\n {ItemHelpers.text_message_output(event.item)}")
            else:
                pass  # Ignore other event types

    print("=== Run complete ===")


if __name__ == "__main__":
    asyncio.run(main())
```