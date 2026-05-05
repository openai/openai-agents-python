---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 두 가지 결과 타입 중 하나를 받습니다.

-   `Runner.run(...)` 또는 `Runner.run_sync(...)`의 [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)`의 [`RunResultStreaming`][agents.result.RunResultStreaming]

둘 다 [`RunResultBase`][agents.result.RunResultBase]를 상속하며, `final_output`, `new_items`, `last_agent`, `raw_responses`, `to_state()` 같은 공통 결과 표면을 노출합니다.

`RunResultStreaming`은 [`stream_events()`][agents.result.RunResultStreaming.stream_events], [`current_agent`][agents.result.RunResultStreaming.current_agent], [`is_complete`][agents.result.RunResultStreaming.is_complete], [`cancel(...)`][agents.result.RunResultStreaming.cancel] 같은 스트리밍 전용 제어를 추가합니다.

## 적절한 결과 표면 선택

대부분의 애플리케이션에는 몇 가지 결과 속성이나 헬퍼만 필요합니다.

| 필요한 경우... | 사용 |
| --- | --- |
| 사용자에게 보여줄 최종 답변 | `final_output` |
| 전체 로컬 transcript가 포함된, 재생 준비가 된 다음 턴 입력 목록 | `to_input_list()` |
| 에이전트, 도구, 핸드오프, 승인 메타데이터가 포함된 풍부한 실행 항목 | `new_items` |
| 일반적으로 다음 사용자 턴을 처리해야 하는 에이전트 | `last_agent` |
| `previous_response_id`를 사용한 OpenAI Responses API 체이닝 | `last_response_id` |
| 대기 중인 승인과 재개 가능한 스냅샷 | `interruptions` 및 `to_state()` |
| 현재 중첩된 `Agent.as_tool()` 호출에 대한 메타데이터 | `agent_tool_invocation` |
| 원문 모델 호출 또는 가드레일 진단 | `raw_responses` 및 가드레일 결과 배열 |

## 최종 출력

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 들어 있습니다. 이는 다음 중 하나입니다.

-   마지막 에이전트에 정의된 `output_type`이 없는 경우 `str`
-   마지막 에이전트에 정의된 출력 타입이 있는 경우 `last_agent.output_type` 타입의 객체
-   예를 들어 승인 인터럽션(중단 처리)에서 일시 중지되어 최종 출력이 생성되기 전에 실행이 중단된 경우 `None`

!!! note

    `final_output`은 `Any`로 타입이 지정되어 있습니다. 핸드오프는 어떤 에이전트가 실행을 완료하는지 바꿀 수 있으므로, SDK는 가능한 전체 출력 타입 집합을 정적으로 알 수 없습니다.

스트리밍 모드에서는 스트림 처리가 완료될 때까지 `final_output`이 `None`으로 유지됩니다. 이벤트별 흐름은 [스트리밍](streaming.md)을 참조하세요.

## 입력, 다음 턴 히스토리, 새 항목

이 표면들은 서로 다른 질문에 답합니다.

| 속성 또는 헬퍼 | 포함 내용 | 최적의 용도 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 이 실행 구간의 기본 입력입니다. 핸드오프 입력 필터가 히스토리를 다시 썼다면, 실행이 계속된 필터링된 입력을 반영합니다. | 이 실행이 실제로 입력으로 사용한 내용을 감사 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 실행의 입력 항목 뷰입니다. 기본 `mode="preserve_all"`은 `new_items`에서 변환된 전체 히스토리를 유지합니다. `mode="normalized"`는 핸드오프 필터링이 모델 히스토리를 다시 쓸 때 표준 계속 입력을 우선합니다. | 수동 채팅 루프, 클라이언트 관리 대화 상태, 일반 항목 히스토리 검사 |
| [`new_items`][agents.result.RunResultBase.new_items] | 에이전트, 도구, 핸드오프, 승인 메타데이터가 포함된 풍부한 [`RunItem`][agents.items.RunItem] 래퍼입니다. | 로그, UI, 감사, 디버깅 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 실행 중 각 모델 호출에서 나온 원문 [`ModelResponse`][agents.items.ModelResponse] 객체입니다. | 제공자 수준 진단 또는 원문 응답 검사 |

실제로는 다음과 같습니다.

-   실행의 일반 입력 항목 뷰가 필요할 때는 `to_input_list()`를 사용하세요.
-   핸드오프 필터링 또는 중첩 핸드오프 히스토리 재작성 후 다음 `Runner.run(..., input=...)` 호출을 위한 표준 로컬 입력이 필요할 때는 `to_input_list(mode="normalized")`를 사용하세요.
-   SDK가 히스토리를 로드하고 저장해 주기를 원할 때는 [`session=...`](sessions/index.md)을 사용하세요.
-   `conversation_id` 또는 `previous_response_id`로 OpenAI 서버 관리 상태를 사용하는 경우, 일반적으로 `to_input_list()`를 다시 보내는 대신 새 사용자 입력만 전달하고 저장된 ID를 재사용하세요.
-   로그, UI, 감사에 사용할 전체 변환 히스토리가 필요할 때는 기본 `to_input_list()` 모드 또는 `new_items`를 사용하세요.

JavaScript SDK와 달리 Python은 모델 형태의 델타만을 위한 별도의 `output` 속성을 노출하지 않습니다. SDK 메타데이터가 필요할 때는 `new_items`를 사용하고, 원문 모델 페이로드가 필요할 때는 `raw_responses`를 검사하세요.

컴퓨터 도구 재생은 원문 Responses 페이로드 형태를 따릅니다. 프리뷰 모델 `computer_call` 항목은 단일 `action`을 보존하는 반면, `gpt-5.5` 컴퓨터 호출은 배치된 `actions[]`를 보존할 수 있습니다. [`to_input_list()`][agents.result.RunResultBase.to_input_list]와 [`RunState`][agents.run_state.RunState]는 모델이 생성한 형태를 그대로 유지하므로, 수동 재생, 일시 중지/재개 흐름, 저장된 transcript가 프리뷰 및 GA 컴퓨터 도구 호출 모두에서 계속 작동합니다. 로컬 실행 결과는 여전히 `new_items`에 `computer_call_output` 항목으로 표시됩니다.

### 새 항목

[`new_items`][agents.result.RunResultBase.new_items]는 실행 중 일어난 일을 가장 풍부하게 보여줍니다. 일반적인 항목 타입은 다음과 같습니다.

-   어시스턴트 메시지용 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   추론 항목용 [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses 도구 검색 요청 및 로드된 도구 검색 결과용 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 및 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   도구 호출 및 그 결과용 [`ToolCallItem`][agents.items.ToolCallItem] 및 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   승인 대기 중 일시 중지된 도구 호출용 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   핸드오프 요청 및 완료된 전환용 [`HandoffCallItem`][agents.items.HandoffCallItem] 및 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

에이전트 연결, 도구 출력, 핸드오프 경계 또는 승인 경계가 필요할 때마다 `to_input_list()` 대신 `new_items`를 선택하세요.

호스티드 툴 검색을 사용할 때는 모델이 내보낸 검색 요청을 보려면 `ToolSearchCallItem.raw_item`을 검사하고, 해당 턴에 로드된 네임스페이스, 함수 또는 호스티드 MCP 서버를 보려면 `ToolSearchOutputItem.raw_item`을 검사하세요.

## 대화 계속 또는 재개

### 다음 턴 에이전트

[`last_agent`][agents.result.RunResultBase.last_agent]에는 마지막으로 실행된 에이전트가 들어 있습니다. 이는 핸드오프 후 다음 사용자 턴에 재사용하기 가장 좋은 에이전트인 경우가 많습니다.

스트리밍 모드에서는 [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent]가 실행 진행에 따라 업데이트되므로, 스트림이 끝나기 전에 핸드오프를 관찰할 수 있습니다.

### 인터럽션(중단 처리) 및 실행 상태

도구에 승인이 필요한 경우, 대기 중인 승인은 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 또는 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. 여기에는 직접 도구, 핸드오프 후 도달한 도구 또는 중첩 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 발생한 승인이 포함될 수 있습니다.

[`to_state()`][agents.result.RunResult.to_state]를 호출해 재개 가능한 [`RunState`][agents.run_state.RunState]를 캡처하고, 대기 중인 항목을 승인하거나 거부한 다음 `Runner.run(...)` 또는 `Runner.run_streamed(...)`로 재개하세요.

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="Use tools when needed.")
result = await Runner.run(agent, "Delete temp files that are no longer needed.")

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state)
```

스트리밍 실행의 경우 먼저 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 소비를 완료한 다음 `result.interruptions`를 검사하고 `result.to_state()`에서 재개하세요. 전체 승인 흐름은 [휴먼인더루프 (HITL)](human_in_the_loop.md)를 참조하세요.

### 서버 관리 계속

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 실행의 최신 모델 응답 ID입니다. OpenAI Responses API 체인을 계속하려면 다음 턴에서 `previous_response_id`로 다시 전달하세요.

이미 `to_input_list()`, `session` 또는 `conversation_id`로 대화를 계속하고 있다면 일반적으로 `last_response_id`가 필요하지 않습니다. 다단계 실행의 모든 모델 응답이 필요하다면 대신 `raw_responses`를 검사하세요.

## Agent-as-tool 메타데이터

결과가 중첩 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 온 경우, [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]은 외부 도구 호출에 대한 불변 메타데이터를 노출합니다.

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

일반적인 최상위 실행의 경우 `agent_tool_invocation`은 `None`입니다.

이는 중첩 결과를 후처리하는 동안 외부 도구 이름, 호출 ID 또는 원문 인수가 필요할 수 있는 `custom_output_extractor` 내부에서 특히 유용합니다. 관련 `Agent.as_tool()` 패턴은 [도구](tools.md)를 참조하세요.

해당 중첩 실행의 파싱된 structured input도 필요하다면 `context_wrapper.tool_input`을 읽으세요. 이는 [`RunState`][agents.run_state.RunState]가 중첩 도구 입력을 일반적으로 직렬화하는 필드이며, `agent_tool_invocation`은 현재 중첩 호출에 대한 실시간 결과 접근자입니다.

## 스트리밍 수명 주기 및 진단

[`RunResultStreaming`][agents.result.RunResultStreaming]은 위와 동일한 결과 표면을 상속하지만, 스트리밍 전용 제어를 추가합니다.

-   의미론적 스트림 이벤트를 소비하기 위한 [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   실행 중 활성 에이전트를 추적하기 위한 [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   스트리밍된 실행이 완전히 완료되었는지 확인하기 위한 [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   실행을 즉시 또는 현재 턴 이후 중지하기 위한 [`cancel(...)`][agents.result.RunResultStreaming.cancel]

비동기 이터레이터가 끝날 때까지 `stream_events()` 소비를 계속하세요. 스트리밍 실행은 해당 이터레이터가 종료되기 전까지 완료되지 않으며, `final_output`, `interruptions`, `raw_responses` 같은 요약 속성과 세션 지속성 부작용은 마지막으로 보이는 토큰이 도착한 후에도 아직 정리 중일 수 있습니다.

`cancel()`을 호출했다면 취소와 정리가 올바르게 완료될 수 있도록 `stream_events()` 소비를 계속하세요.

Python은 별도의 스트리밍된 `completed` promise나 `error` 속성을 노출하지 않습니다. 최종 스트리밍 실패는 `stream_events()`에서 예외를 발생시키는 방식으로 표시되며, `is_complete`는 실행이 최종 상태에 도달했는지 여부를 반영합니다.

### 원문 응답

[`raw_responses`][agents.result.RunResultBase.raw_responses]에는 실행 중 수집된 원문 모델 응답이 들어 있습니다. 다단계 실행은 예를 들어 핸드오프 또는 반복되는 모델/도구/모델 사이클 전반에서 둘 이상의 응답을 생성할 수 있습니다.

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 `raw_responses`의 마지막 항목에서 나온 ID일 뿐입니다.

### 가드레일 결과

에이전트 수준 가드레일은 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 및 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results]로 노출됩니다.

도구 가드레일은 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]로 별도로 노출됩니다.

이 배열들은 실행 전반에 걸쳐 누적되므로, 의사결정 로깅, 추가 가드레일 메타데이터 저장 또는 실행이 차단된 이유 디버깅에 유용합니다.

### 컨텍스트 및 사용량

[`context_wrapper`][agents.result.RunResultBase.context_wrapper]는 승인, 사용량, 중첩 `tool_input` 같은 SDK 관리 런타임 메타데이터와 함께 앱 컨텍스트를 노출합니다.

사용량은 `context_wrapper.usage`에서 추적됩니다. 스트리밍 실행의 경우 사용량 합계는 스트림의 마지막 청크가 처리될 때까지 지연될 수 있습니다. 전체 래퍼 형태와 지속성 관련 주의사항은 [컨텍스트 관리](context.md)를 참조하세요.