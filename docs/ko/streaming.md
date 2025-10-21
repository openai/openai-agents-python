---
search:
  exclude: true
---
# 스트리밍

스트리밍을 사용하면 에이전트 실행이 진행되는 동안 업데이트를 구독할 수 있습니다. 이는 최종 사용자에게 진행 상황과 부분 응답을 보여주는 데 유용합니다.

스트리밍하려면 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]를 호출하여 [`RunResultStreaming`][agents.result.RunResultStreaming]을 받을 수 있습니다. `result.stream_events()`를 호출하면 아래에 설명된 [`StreamEvent`][agents.stream_events.StreamEvent] 객체의 비동기 스트림을 제공합니다.

## 원문 응답 이벤트

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent]는 LLM에서 직접 전달되는 원문 이벤트입니다. OpenAI Responses API 형식이며, 각 이벤트에는 타입(예: `response.created`, `response.output_text.delta` 등)과 데이터가 있습니다. 생성되는 즉시 사용자에게 응답 메시지를 스트리밍하려는 경우 유용합니다.

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

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent]는 더 높은 수준의 이벤트입니다. 항목이 완전히 생성되었을 때 알려줍니다. 이를 통해 각 토큰이 아닌 "메시지 생성됨", "도구 실행됨" 등의 수준에서 진행 상태를 전달할 수 있습니다. 유사하게, [`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent]는 현재 에이전트가 변경될 때 업데이트를 제공합니다(예: 핸드오프의 결과로).

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

## 도구 출력 스트리밍 이벤트

[`ToolOutputStreamEvent`][agents.stream_events.ToolOutputStreamEvent]를 사용하면 도구가 실행되는 동안 증분 출력을 받을 수 있습니다. 이는 장시간 실행되는 도구에서 사용자에게 실시간으로 진행 상황을 표시하려는 경우에 유용합니다.

스트리밍 도구를 만들려면 문자열 청크를 yield하는 비동기 제너레이터 함수를 정의하세요:

```python
import asyncio
from collections.abc import AsyncIterator
from agents import Agent, Runner, ToolOutputStreamEvent, function_tool

@function_tool
async def search_documents(query: str) -> AsyncIterator[str]:
    """문서를 검색하고 발견된 결과를 스트리밍합니다."""
    documents = [
        f"문서 1에는 {query}에 대한 정보가 포함되어 있습니다...\n",
        f"문서 2에는 {query}에 대한 추가 세부정보가 있습니다...\n",
        f"문서 3은 {query}에 대한 분석을 제공합니다...\n",
    ]
    
    for doc in documents:
        # 처리 시간 시뮬레이션
        await asyncio.sleep(0.5)
        # 증분 결과 yield
        yield doc


async def main():
    agent = Agent(
        name="Research Assistant",
        instructions="당신은 사용자가 정보를 검색하도록 돕습니다.",
        tools=[search_documents],
    )

    result = Runner.run_streamed(
        agent,
        input="AI에 관한 정보를 검색하세요",
    )

    async for event in result.stream_events():
        # 도구 스트리밍 이벤트 처리
        if event.type == "tool_output_stream_event":
            print(f"[{event.tool_name}] {event.delta}", end="", flush=True)
        # 최종 도구 출력 처리
        elif event.type == "run_item_stream_event" and event.name == "tool_output":
            print(f"\n✓ 도구 완료\n")


if __name__ == "__main__":
    asyncio.run(main())
```

스트리밍 도구에 대한 주요 사항:

- 스트리밍 도구는 `AsyncIterator[str]`(문자열을 yield하는 비동기 제너레이터)을 반환해야 합니다
- yield된 각 청크는 `ToolOutputStreamEvent`로 발행됩니다
- 모든 청크는 자동으로 누적되어 최종 도구 출력으로 LLM에 전송됩니다
- 비스트리밍 도구는 스트리밍 도구와 함께 정상적으로 작동합니다
- 비스트리밍 모드(`Runner.run()`)에서 스트리밍 도구는 반환하기 전에 모든 청크를 자동으로 수집합니다