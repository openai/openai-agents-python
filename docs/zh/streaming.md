---
search:
  exclude: true
---
# 流式传输

流式传输让你可以在智能体运行过程中订阅其更新。这对于向终端用户展示进度更新和部分响应很有帮助。

要进行流式传输，你可以调用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，它会返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。调用 `result.stream_events()` 会得到一个由 [`StreamEvent`][agents.stream_events.StreamEvent] 对象组成的异步流，下面会进行说明。

持续消费 `result.stream_events()`，直到异步迭代器结束。流式运行在迭代器结束前都不算完成，而且诸如会话持久化、审批记录或历史压缩等后处理，可能会在最后一个可见 token 到达后才完成。循环退出时，`result.is_complete` 会反映最终运行状态。

## 原始响应事件

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 是直接从 LLM 透传的原始事件。它们采用 OpenAI Responses API 格式，这意味着每个事件都有类型（如 `response.created`、`response.output_text.delta` 等）和数据。如果你希望在响应消息生成后立即流式发送给用户，这些事件会很有用。

计算机工具原始事件与存储结果一样，保持 preview 与 GA 的区分。Preview 流会流式返回带有单个 `action` 的 `computer_call` 项，而 `gpt-5.4` 可以流式返回带有批量 `actions[]` 的 `computer_call` 项。更高层的 [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 接口不会为此增加专用的计算机事件名：这两种形态仍都会以 `tool_called` 呈现，而截图结果会以封装了 `computer_call_output` 项的 `tool_output` 返回。

例如，下面将按 token 逐个输出 LLM 生成的文本。

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

流式传输与因工具审批而暂停的运行兼容。如果某个工具需要审批，`result.stream_events()` 会结束，待处理的审批会暴露在 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中。将结果通过 `result.to_state()` 转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝该中断，然后使用 `Runner.run_streamed(...)` 恢复运行。

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

完整的暂停/恢复流程请参见[人类参与指南](human_in_the_loop.md)。

## 在当前轮次后取消流式传输

如果你需要在中途停止一次流式运行，调用 [`result.cancel()`][agents.result.RunResultStreaming.cancel]。默认会立即停止运行。若想在停止前让当前轮次完整结束，请改用 `result.cancel(mode="after_turn")`。

在 `result.stream_events()` 结束前，流式运行都不算完成。SDK 可能仍在最后一个可见 token 之后持久化会话项、完成审批状态收尾或压缩历史。

如果你是基于 [`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] 手动继续，且 `cancel(mode="after_turn")` 在工具轮次后停止，请用该 normalized 输入重新运行 `result.last_agent` 以继续未完成轮次，而不是立即追加新的用户轮次。
-   如果一次流式运行因工具审批而停止，不要将其视为新轮次。先完成流的消费，检查 `result.interruptions`，然后改为从 `result.to_state()` 恢复。
-   使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 自定义在下一次模型调用前，如何合并检索到的会话历史与新的用户输入。如果你在其中改写了新轮次项，被改写后的版本将作为该轮次的持久化内容。

## 运行项事件与智能体事件

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 是更高层级的事件。它会在某个项完整生成后通知你。这样你就可以在“消息已生成”“工具已运行”等层级推送进度更新，而不是按 token 推送。类似地，[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] 会在当前智能体发生变化时提供更新（例如因任务转移导致的变化）。

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

出于向后兼容考虑，`handoff_occured` 保留了故意的拼写错误。

当你使用托管工具搜索时，模型发出工具搜索请求会触发 `tool_search_called`，Responses API 返回已加载子集时会触发 `tool_search_output_created`。

例如，下面会忽略原始事件，并向用户流式推送更新。

```python
import asyncio
import random
from agents import Agent, ItemHelpers, Runner, function_tool

@function_tool
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