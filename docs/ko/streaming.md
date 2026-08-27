---
search:
  exclude: true
---
# 스트리밍

스트리밍을 사용하면 에이전트 실행이 진행되는 동안 업데이트를 구독할 수 있습니다. 최종 사용자에게 진행 상황 업데이트와 부분 응답을 표시할 때 유용합니다.

스트리밍하려면 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]을 호출하여 [`RunResultStreaming`][agents.result.RunResultStreaming]을 받을 수 있습니다. `result.stream_events()`를 호출하면 아래에서 설명하는 [`StreamEvent`][agents.stream_events.StreamEvent] 객체의 비동기 스트림을 얻습니다.

비동기 이터레이터가 완료될 때까지 `result.stream_events()`를 계속 소비해야 합니다. 스트리밍 실행은 이터레이터가 종료될 때까지 완료된 것이 아니며, 세션 영속화, 승인 기록 관리, 기록 압축과 같은 후처리는 마지막으로 표시되는 토큰이 도착한 후에도 계속될 수 있습니다. 루프가 종료되면 `result.is_complete`에 최종 실행 상태가 반영됩니다.

## 가공되지 않은 응답 이벤트 {#raw-response-events}

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 객체는 LLM에서 직접 전달된 가공되지 않은 이벤트를 래핑합니다. 각 객체의 `data` 필드에는 `response.created` 또는 `response.output_text.delta` 같은 유형의 OpenAI Responses API 이벤트가 포함됩니다. 이러한 이벤트는 응답 메시지가 생성되는 즉시 사용자에게 스트리밍하려는 경우 유용합니다.

컴퓨터 도구의 가공되지 않은 이벤트는 저장된 결과와 동일하게 프리뷰와 GA를 구분합니다. 프리뷰 흐름은 하나의 `action`이 있는 `computer_call` 항목을 스트리밍하는 반면, `gpt-5.5`는 일괄 처리된 `actions[]`가 있는 `computer_call` 항목을 스트리밍할 수 있습니다. 상위 수준의 [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 인터페이스에는 이를 위한 컴퓨터 전용 이벤트 이름이 별도로 추가되지 않습니다. 두 형식 모두 계속 `tool_called`으로 노출되며, 스크린샷 결과는 `computer_call_output` 항목을 래핑하는 `tool_output`로 반환됩니다.

예를 들어 다음 코드는 LLM이 생성한 텍스트를 토큰 단위로 출력합니다.

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

## 스트리밍 및 승인 {#streaming-and-approvals}

스트리밍은 도구 승인을 위해 일시 중지되는 실행과 호환됩니다. 도구에 승인이 필요하면 `result.stream_events()`가 완료되고, 보류 중인 승인은 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. `result.to_state()`를 사용하여 결과를 [`RunState`][agents.run_state.RunState]로 변환하고, 인터럽션(중단 처리)을 승인하거나 거부한 다음 `Runner.run_streamed(...)`으로 재개합니다.

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

전체 일시 중지 및 재개 과정은 [휴먼인더루프 (HITL) 가이드](human_in_the_loop.md)를 참고하세요.

## 현재 턴 이후 스트리밍 취소 {#cancel-streaming-after-the-current-turn}

진행 중인 스트리밍 실행을 중간에 중지해야 하는 경우 [`result.cancel()`][agents.result.RunResultStreaming.cancel]을 호출합니다. 기본적으로 실행이 즉시 중지됩니다. 중지하기 전에 현재 턴이 정상적으로 완료되도록 하려면 대신 `result.cancel(mode="after_turn")`를 호출합니다.

스트리밍 실행은 `result.stream_events()`가 완료될 때까지 완료된 것이 아닙니다. 마지막으로 표시되는 토큰 이후에도 SDK에서 세션 항목을 영속화하거나, 승인 상태를 확정하거나, 기록을 압축하고 있을 수 있습니다.

[`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list]에서 수동으로 계속 진행하는 중에 도구 턴 이후 `cancel(mode="after_turn")`이 중지된 경우, 곧바로 새 사용자 턴을 추가하지 말고 정규화된 해당 입력으로 `result.last_agent`를 다시 실행하여 완료되지 않은 기존 사용자 턴을 계속합니다.

-   완료되지 않은 실행이 재개되기 전에 새 사용자 입력이 도착하면, 끝까지 소비한 결과를 `result.to_state()`으로 변환하고 [`state.add_input(...)`][agents.run_state.RunState.add_input]을 호출한 후 해당 상태에서 재개합니다. 러너는 다음 모델 호출 직전에 준비된 입력을 반영합니다. [재개 전 입력 추가](results.md#add-input-before-resuming)를 참고하세요.
-   스트리밍 실행이 도구 승인을 위해 중지된 경우 이를 새 턴으로 취급하지 마세요. 스트림을 끝까지 소비하고 `result.interruptions`를 검사한 다음 `result.to_state()`에서 재개합니다.
-   다음 모델 호출 전에 조회된 세션 기록과 새 사용자 입력을 병합하는 방식을 사용자 지정하려면 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback]을 사용합니다. 여기에서 새 턴 항목을 다시 작성하면 다시 작성된 버전이 해당 턴에 영속화됩니다.

## 실행 항목 이벤트 및 에이전트 이벤트 {#run-item-events-and-agent-events}

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent]는 상위 수준의 이벤트입니다. 항목이 완전히 생성되었을 때 이를 알려 줍니다. 따라서 각 토큰 대신 "메시지 생성됨", "도구 실행됨" 등의 수준으로 진행 상황 업데이트를 전달할 수 있습니다. 마찬가지로 [`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent]는 현재 에이전트가 변경될 때 업데이트를 제공합니다(예: 핸드오프의 결과).

### 실행 항목 이벤트 이름 {#run-item-event-names}

`RunItemStreamEvent.name`는 정해진 의미론적 이벤트 이름 집합을 사용합니다.

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

`handoff_occured`는 이전 버전과의 호환성을 위해 의도적으로 철자가 잘못 표기되어 있습니다.

핸드오프 호출은 `handoff_requested`으로만 내보내지며, `tool_called`로도 내보내지는 것은 아닙니다. 동일한 턴의 일반 함수 도구 호출은 계속 `tool_called`를 내보냅니다.

호스티드 툴 검색을 사용하면 모델에서 도구 검색 요청을 실행할 때 `tool_search_called`이 내보내지고, Responses API에서 로드된 하위 집합을 반환할 때 `tool_search_output_created`가 내보내집니다.

프로그래밍 방식 도구 호출을 사용하면 생성된 `program`과 프로그램이 소유한 일반 하위 도구 호출에 대해 `tool_called`가 내보내집니다. 하위 도구 출력과 생성된 `program`와 일치하는 `program_output`에 대해서는 `tool_output`이 내보내집니다. 프로그램이 소유한 호스티드 MCP `mcp_approval_request` 및 `mcp_list_tools` 항목은 예외입니다. 이들은 각각 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem] 및 [`MCPListToolsItem`][agents.items.MCPListToolsItem]을 래핑하는 `mcp_approval_requested` 및 `mcp_list_tools`으로 내보내집니다. 나머지 항목을 구분하려면 가공되지 않은 항목의 `type`을 검사하세요. 프로그램이 소유한 하위 호출에는 유형이 `program`이고 호출자 ID가 상위 프로그램을 식별하는 `caller`도 포함됩니다.

예를 들어 다음 코드는 가공되지 않은 이벤트를 무시하고 사용자에게 업데이트를 스트리밍합니다.

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