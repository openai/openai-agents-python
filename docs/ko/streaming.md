---
search:
  exclude: true
---
# 스트리밍

스트리밍을 사용하면 에이전트 실행이 진행되는 동안 업데이트를 구독할 수 있습니다. 이는 최종 사용자에게 진행 상황 업데이트와 부분 응답을 보여줄 때 유용할 수 있습니다.

스트리밍하려면 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]를 호출하면 되며, 그러면 [`RunResultStreaming`][agents.result.RunResultStreaming]을 받습니다. `result.stream_events()`를 호출하면 아래에 설명된 [`StreamEvent`][agents.stream_events.StreamEvent] 객체의 비동기 스트림을 얻을 수 있습니다.

비동기 이터레이터가 끝날 때까지 `result.stream_events()`를 계속 소비하세요. 스트리밍 실행은 이터레이터가 종료될 때까지 완료된 것이 아니며, 세션 영속화, 승인 장부 처리, 히스토리 압축 같은 후처리는 마지막으로 보이는 토큰이 도착한 뒤에 끝날 수 있습니다. 루프가 종료되면 `result.is_complete`가 최종 실행 상태를 반영합니다.

## 원시 응답 이벤트

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent]는 LLM에서 직접 전달되는 원시 이벤트입니다. OpenAI Responses API 형식이므로 각 이벤트에는 타입(예: `response.created`, `response.output_text.delta` 등)과 데이터가 있습니다. 이러한 이벤트는 생성되는 즉시 사용자에게 응답 메시지를 스트리밍하려는 경우에 유용합니다.

컴퓨터 도구 원시 이벤트는 저장된 결과와 동일한 프리뷰-vs-GA 구분을 유지합니다. 프리뷰 플로는 하나의 `action`이 있는 `computer_call` 항목을 스트리밍하는 반면, `gpt-5.5`는 일괄 처리된 `actions[]`가 있는 `computer_call` 항목을 스트리밍할 수 있습니다. 더 높은 수준의 [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 표면은 이를 위해 컴퓨터 전용 특별 이벤트 이름을 추가하지 않습니다. 두 형태 모두 여전히 `tool_called`로 노출되며, 스크린샷 결과는 `computer_call_output` 항목을 감싼 `tool_output`으로 반환됩니다.

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

## 스트리밍과 승인

스트리밍은 도구 승인을 위해 일시 중지되는 실행과 호환됩니다. 도구에 승인이 필요한 경우 `result.stream_events()`가 종료되고 보류 중인 승인은 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. `result.to_state()`로 결과를 [`RunState`][agents.run_state.RunState]로 변환하고, 인터럽션(중단 처리)을 승인하거나 거부한 다음 `Runner.run_streamed(...)`로 재개하세요.

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

전체 일시 중지/재개 절차는 [휴먼인더루프 가이드](human_in_the_loop.md)를 참조하세요.

## 현재 턴 이후 스트리밍 취소

스트리밍 실행을 중간에 중지해야 하는 경우 [`result.cancel()`][agents.result.RunResultStreaming.cancel]을 호출하세요. 기본적으로 이는 실행을 즉시 중지합니다. 중지하기 전에 현재 턴이 깔끔하게 완료되도록 하려면 대신 `result.cancel(mode="after_turn")`을 호출하세요.

스트리밍된 실행은 `result.stream_events()`가 끝날 때까지 완료되지 않습니다. 마지막으로 보이는 토큰 이후에도 SDK가 세션 항목을 영속화하거나, 승인 상태를 마무리하거나, 히스토리를 압축하고 있을 수 있습니다.

[`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list]에서 수동으로 계속 진행하고 있으며 `cancel(mode="after_turn")`이 도구 턴 이후 중지되는 경우, 즉시 새 사용자 턴을 추가하는 대신 해당 정규화된 입력으로 `result.last_agent`를 다시 실행하여 완료되지 않은 턴을 계속하세요.
-   스트리밍 실행이 도구 승인 때문에 중지된 경우, 이를 새 턴으로 취급하지 마세요. 스트림을 끝까지 소진하고 `result.interruptions`를 검사한 다음 `result.to_state()`에서 재개하세요.
-   다음 모델 호출 전에 가져온 세션 히스토리와 새 사용자 입력을 병합하는 방식을 사용자 지정하려면 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback]을 사용하세요. 그곳에서 새 턴 항목을 다시 작성하면, 다시 작성된 버전이 해당 턴에 대해 영속화됩니다.

## 실행 항목 이벤트 및 에이전트 이벤트

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent]는 더 높은 수준의 이벤트입니다. 항목이 완전히 생성되었을 때 알려줍니다. 이를 통해 각 토큰이 아니라 "메시지 생성됨", "도구 실행됨" 등의 수준에서 진행 상황 업데이트를 푸시할 수 있습니다. 마찬가지로 [`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent]는 현재 에이전트가 변경될 때(예: 핸드오프의 결과) 업데이트를 제공합니다.

### 실행 항목 이벤트 이름

`RunItemStreamEvent.name`은 고정된 의미론적 이벤트 이름 집합을 사용합니다.

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

`handoff_occured`는 하위 호환성을 위해 의도적으로 철자가 잘못되어 있습니다.

호스티드 툴 검색을 사용할 때는 모델이 도구 검색 요청을 발행하면 `tool_search_called`가 내보내지고, Responses API가 로드된 하위 집합을 반환하면 `tool_search_output_created`가 내보내집니다.

예를 들어, 다음은 원시 이벤트를 무시하고 사용자에게 업데이트를 스트리밍합니다.

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