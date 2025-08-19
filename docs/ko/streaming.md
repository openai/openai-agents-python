---
search:
  exclude: true
---
# 스트리밍

스트리밍을 사용하면 에이전트 실행 진행 상황의 업데이트를 구독할 수 있습니다. 이는 최종 사용자에게 진행 상황과 부분 응답을 보여주는 데 유용합니다.

스트리밍하려면 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]를 호출하면 [`RunResultStreaming`][agents.result.RunResultStreaming]을 받습니다. `result.stream_events()`를 호출하면 아래에 설명된 [`StreamEvent`][agents.stream_events.StreamEvent] 객체의 비동기 스트림을 얻습니다.

## 원문 응답 이벤트

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent]는 LLM에서 직접 전달되는 원문 이벤트입니다. OpenAI Responses API 형식이며, 각 이벤트에는 타입(`response.created`, `response.output_text.delta` 등)과 데이터가 있습니다. 이 이벤트는 생성 즉시 사용자에게 응답 메시지를 스트리밍하려는 경우에 유용합니다.

예를 들어, 다음은 LLM이 생성한 텍스트를 토큰 단위로 출력합니다.

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

## 실행 항목 이벤트와 에이전트 이벤트

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent]는 상위 수준 이벤트입니다. 항목이 완전히 생성되었을 때 알려줍니다. 이를 통해 각 토큰 대신 "메시지 생성됨", "도구 실행됨" 등의 수준에서 진행 상황 업데이트를 전달할 수 있습니다. 이와 유사하게, [`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent]는 현재 에이전트가 변경될 때(예: 핸드오프 결과) 업데이트를 제공합니다.

예를 들어, 다음은 원문 이벤트를 무시하고 사용자에게 업데이트를 스트리밍합니다.

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