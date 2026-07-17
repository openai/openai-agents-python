---
search:
  exclude: true
---
# 流式传输

流式传输允许你在智能体运行期间订阅其更新。这对于向最终用户展示进度更新和部分响应非常有用。

要使用流式传输，可以调用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，它会返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。调用 `result.stream_events()` 会得到由 [`StreamEvent`][agents.stream_events.StreamEvent] 对象组成的异步流，下文将对其进行说明。

应持续消费 `result.stream_events()`，直到异步迭代器结束。流式运行只有在迭代器结束后才算完成；会话持久化、审批记录处理或历史记录压缩等后处理操作，可能会在最后一个可见 token 到达后才完成。循环退出时，`result.is_complete` 会反映运行的最终状态。

## 原始响应事件

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 是直接从 LLM 传递的原始事件。它们采用 OpenAI Responses API格式，这意味着每个事件都有类型（例如 `response.created`、`response.output_text.delta` 等）和数据。如果你希望在响应消息生成后立即以流式方式发送给用户，这些事件会非常有用。

计算机工具的原始事件与已存储结果一样，会保留预览版与正式版（GA）之间的区别。预览版流程会流式传输包含单个 `action` 的 `computer_call` 项，而 `gpt-5.5` 可以流式传输包含批量 `actions[]` 的 `computer_call` 项。更高层级的 [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 接口不会为此添加计算机工具专用的特殊事件名称：两种形式仍然都以 `tool_called` 呈现，而截图结果则以 `tool_output` 返回，其中封装了一个 `computer_call_output` 项。

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

## 流式传输与审批

流式传输兼容因等待工具审批而暂停的运行。如果某个工具需要审批，`result.stream_events()` 会结束，待处理的审批将通过 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 提供。使用 `result.to_state()` 将结果转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝中断项，然后通过 `Runner.run_streamed(...)` 恢复运行。

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

有关完整的暂停/恢复流程，请参阅[人工介入指南](human_in_the_loop.md)。

## 当前轮次结束后的流式传输取消

如果需要中途停止流式运行，请调用 [`result.cancel()`][agents.result.RunResultStreaming.cancel]。默认情况下，这会立即停止运行。若要让当前轮次完整结束后再停止，请改为调用 `result.cancel(mode="after_turn")`。

流式运行只有在 `result.stream_events()` 结束后才算完成。在最后一个可见 token 到达后，SDK 可能仍在持久化会话项、完成审批状态处理或压缩历史记录。

如果你要手动基于 [`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] 继续运行，并且 `cancel(mode="after_turn")` 在某个工具轮次后停止，请使用该规范化输入重新运行 `result.last_agent`，以继续尚未完成的轮次，而不要立即追加新的用户轮次。
-   如果流式运行因等待工具审批而停止，请勿将其视为新的轮次。应完整消费流、检查 `result.interruptions`，然后从 `result.to_state()` 恢复运行。
-   使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 自定义在下一次模型调用前，如何合并检索到的会话历史记录与新的用户输入。如果你在此处重写了新轮次中的项目，该轮次将持久化重写后的版本。

## 运行项事件与智能体事件

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 是更高层级的事件。它们会在某个项目完全生成后通知你。这样，你就可以按“消息已生成”“工具已运行”等粒度向用户推送进度更新，而不必逐 token 更新。类似地，[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] 会在当前智能体发生变化时向你提供更新（例如，由任务转移引起的变化）。

### 运行项事件名称

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

为保持向后兼容，`handoff_occured` 有意保留了拼写错误。

使用托管工具搜索时，模型发出工具搜索请求会触发 `tool_search_called`，而 Responses API 返回已加载的子集时会触发 `tool_search_output_created`。

使用程序化工具调用时，生成的 `program` 和由程序管理的普通子工具调用都会触发 `tool_called`。子工具输出以及相应的 `program_output` 会触发 `tool_output`。由程序管理的托管 MCP `mcp_approval_request` 和 `mcp_list_tools` 项属于例外：它们分别以 `mcp_approval_requested` 和 `mcp_list_tools` 的形式触发，并分别封装 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem] 和 [`MCPListToolsItem`][agents.items.MCPListToolsItem]。可以检查原始项目的 `type` 来区分其他项目；由程序管理的子调用还带有一个 `caller`，其类型为 `program`，并且其调用方 ID 用于标识父程序。

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