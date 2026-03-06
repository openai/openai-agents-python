---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 두 가지 결과 타입 중 하나를 받습니다:

-   `Runner.run(...)` 또는 `Runner.run_sync(...)`의 [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)`의 [`RunResultStreaming`][agents.result.RunResultStreaming]

두 타입 모두 [`RunResultBase`][agents.result.RunResultBase]를 상속하며, `final_output`, `new_items`, `last_agent`, `raw_responses`, `to_state()` 같은 공통 결과 표면을 제공합니다.

`RunResultStreaming`은 [`stream_events()`][agents.result.RunResultStreaming.stream_events], [`current_agent`][agents.result.RunResultStreaming.current_agent], [`is_complete`][agents.result.RunResultStreaming.is_complete], [`cancel(...)`][agents.result.RunResultStreaming.cancel] 같은 스트리밍 전용 제어 기능을 추가로 제공합니다.

## 적절한 결과 표면 선택

대부분의 애플리케이션은 몇 가지 결과 속성이나 헬퍼만 필요합니다:

| 다음이 필요하다면... | 사용 |
| --- | --- |
| 사용자에게 보여줄 최종 답변 | `final_output` |
| 전체 로컬 전사 기록이 포함된 재생 가능한 다음 턴 입력 목록 | `to_input_list()` |
| 에이전트, 도구, 핸드오프, 승인 메타데이터가 포함된 풍부한 실행 항목 | `new_items` |
| 일반적으로 다음 사용자 턴을 처리해야 하는 에이전트 | `last_agent` |
| `previous_response_id`를 사용하는 OpenAI Responses API 체이닝 | `last_response_id` |
| 보류 중인 승인 및 재개 가능한 스냅샷 | `interruptions` 및 `to_state()` |
| 현재 중첩된 `Agent.as_tool()` 호출에 대한 메타데이터 | `agent_tool_invocation` |
| 원문 모델 호출 또는 가드레일 진단 | `raw_responses` 및 가드레일 결과 배열 |

## 최종 출력

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 들어 있습니다. 이는 다음 중 하나입니다:

-   마지막 에이전트에 `output_type`이 정의되지 않은 경우 `str`
-   마지막 에이전트에 출력 타입이 정의된 경우 `last_agent.output_type` 타입의 객체
-   최종 출력이 생성되기 전에 실행이 중단된 경우 `None`(예: 승인 인터럽션에서 일시 중지된 경우)

!!! note

    `final_output`의 타입은 `Any`입니다. 핸드오프로 인해 어떤 에이전트가 실행을 완료할지 바뀔 수 있으므로, SDK는 가능한 출력 타입 전체를 정적으로 알 수 없습니다.

스트리밍 모드에서는 스트림 처리가 완료될 때까지 `final_output`이 `None`으로 유지됩니다. 이벤트별 흐름은 [Streaming](streaming.md)을 참고하세요.

## 입력, 다음 턴 기록, 새 항목

이 표면들은 서로 다른 질문에 답합니다:

| 속성 또는 헬퍼 | 포함 내용 | 최적 용도 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 이 실행 구간의 기본 입력. 핸드오프 입력 필터가 기록을 다시 쓴 경우, 실행이 이어진 필터링된 입력을 반영합니다 | 이 실행이 실제로 어떤 입력을 사용했는지 감사 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | `input`과 이 실행의 변환된 `new_items`로 구성된 재생 가능한 다음 턴 입력 목록 | 수동 채팅 루프 및 클라이언트 관리 대화 상태 |
| [`new_items`][agents.result.RunResultBase.new_items] | 에이전트, 도구, 핸드오프, 승인 메타데이터가 포함된 풍부한 [`RunItem`][agents.items.RunItem] 래퍼 | 로그, UI, 감사, 디버깅 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 실행 중 각 모델 호출에서 수집된 원문 [`ModelResponse`][agents.items.ModelResponse] 객체 | 제공자 수준 진단 또는 원문 응답 검사 |

실무에서는:

-   애플리케이션이 전체 대화 전사 기록을 수동으로 유지한다면 `to_input_list()`를 사용하세요
-   SDK가 기록을 로드/저장하게 하려면 [`session=...`](sessions/index.md)을 사용하세요
-   `conversation_id` 또는 `previous_response_id`로 OpenAI 서버 관리 상태를 사용 중이라면, 보통은 `to_input_list()`를 다시 보내기보다 새 사용자 입력만 전달하고 저장된 ID를 재사용하세요

JavaScript SDK와 달리 Python은 모델 형태의 델타만 담은 별도 `output` 속성을 제공하지 않습니다. SDK 메타데이터가 필요하면 `new_items`를 사용하고, 원문 모델 페이로드가 필요하면 `raw_responses`를 확인하세요.

### 새 항목

[`new_items`][agents.result.RunResultBase.new_items]는 실행 중 발생한 일을 가장 풍부하게 보여줍니다. 일반적인 항목 타입은 다음과 같습니다:

-   어시스턴트 메시지용 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   추론 항목용 [`ReasoningItem`][agents.items.ReasoningItem]
-   도구 호출 및 결과용 [`ToolCallItem`][agents.items.ToolCallItem] 및 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   승인을 위해 일시 중지된 도구 호출용 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   핸드오프 요청 및 완료된 이관용 [`HandoffCallItem`][agents.items.HandoffCallItem] 및 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

에이전트 연관 정보, 도구 출력, 핸드오프 경계, 승인 경계가 필요하다면 `to_input_list()`보다 `new_items`를 선택하세요.

## 대화 계속 또는 재개

### 다음 턴 에이전트

[`last_agent`][agents.result.RunResultBase.last_agent]에는 마지막으로 실행된 에이전트가 들어 있습니다. 핸드오프 이후 다음 사용자 턴에 재사용할 최적의 에이전트인 경우가 많습니다.

스트리밍 모드에서는 실행이 진행됨에 따라 [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent]가 업데이트되므로, 스트림이 끝나기 전에 핸드오프를 관찰할 수 있습니다.

### 인터럽션 및 실행 상태

도구에 승인이 필요하면 보류 중 승인 항목이 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 또는 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. 여기에는 직접 도구에서 발생한 승인, 핸드오프 이후 도달한 도구에서 발생한 승인, 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 발생한 승인이 포함될 수 있습니다.

[`to_state()`][agents.result.RunResult.to_state]를 호출해 재개 가능한 [`RunState`][agents.run_state.RunState]를 캡처하고, 보류 항목을 승인 또는 거부한 뒤 `Runner.run(...)` 또는 `Runner.run_streamed(...)`로 재개하세요.

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

스트리밍 실행의 경우 먼저 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 소비를 완료한 다음 `result.interruptions`를 확인하고 `result.to_state()`에서 재개하세요. 전체 승인 흐름은 [Human-in-the-loop](human_in_the_loop.md)를 참고하세요.

### 서버 관리 연속 실행

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 실행의 최신 모델 응답 ID입니다. OpenAI Responses API 체인을 이어가려면 다음 턴에서 이를 `previous_response_id`로 다시 전달하세요.

이미 `to_input_list()`, `session`, 또는 `conversation_id`로 대화를 이어가고 있다면 보통 `last_response_id`는 필요하지 않습니다. 다단계 실행의 모든 모델 응답이 필요하면 대신 `raw_responses`를 확인하세요.

## Agent-as-tool 메타데이터

결과가 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 나온 경우, [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]은 외부 도구 호출에 대한 불변 메타데이터를 제공합니다:

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

일반적인 최상위 실행에서는 `agent_tool_invocation`이 `None`입니다.

이는 특히 `custom_output_extractor` 내부에서 유용하며, 중첩 결과를 후처리할 때 외부 도구 이름, 호출 ID, 원문 인자가 필요할 수 있습니다. 주변 `Agent.as_tool()` 패턴은 [Tools](tools.md)를 참고하세요.

해당 중첩 실행의 파싱된 구조화 입력도 필요하다면 `context_wrapper.tool_input`을 읽으세요. 이 필드는 중첩 도구 입력에 대해 [`RunState`][agents.run_state.RunState]가 일반적으로 직렬화하는 필드이며, `agent_tool_invocation`은 현재 중첩 호출에 대한 라이브 결과 접근자입니다.

## 스트리밍 수명 주기 및 진단

[`RunResultStreaming`][agents.result.RunResultStreaming]은 위와 동일한 결과 표면을 상속하면서, 스트리밍 전용 제어 기능을 추가합니다:

-   의미론적 스트림 이벤트를 소비하는 [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   실행 중 활성 에이전트를 추적하는 [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   스트리밍 실행이 완전히 끝났는지 확인하는 [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   실행을 즉시 또는 현재 턴 이후에 중단하는 [`cancel(...)`][agents.result.RunResultStreaming.cancel]

비동기 이터레이터가 끝날 때까지 `stream_events()`를 계속 소비하세요. 스트리밍 실행은 해당 이터레이터가 종료되어야 완료되며, 마지막으로 보이는 토큰이 도착한 뒤에도 `final_output`, `interruptions`, `raw_responses`, 세션 영속화 부작용 같은 요약 속성이 아직 정리 중일 수 있습니다.

`cancel()`을 호출한 경우에도 취소 및 정리가 올바르게 완료되도록 `stream_events()` 소비를 계속하세요.

Python은 별도의 스트리밍 `completed` 프로미스나 `error` 속성을 제공하지 않습니다. 최종 스트리밍 실패는 `stream_events()`에서 예외를 발생시키는 방식으로 노출되며, `is_complete`는 실행이 종료 상태에 도달했는지를 반영합니다.

### 원문 응답

[`raw_responses`][agents.result.RunResultBase.raw_responses]에는 실행 중 수집된 원문 모델 응답이 들어 있습니다. 다단계 실행에서는 예를 들어 핸드오프 또는 반복되는 모델/도구/모델 사이클을 거치며 응답이 둘 이상 생성될 수 있습니다.

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 `raw_responses`의 마지막 항목 ID일 뿐입니다.

### 가드레일 결과

에이전트 수준 가드레일은 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 및 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results]로 노출됩니다.

도구 가드레일은 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]로 별도 노출됩니다.

이 배열들은 실행 전체에 걸쳐 누적되므로, 의사결정 로깅, 추가 가드레일 메타데이터 저장, 실행이 차단된 이유 디버깅에 유용합니다.

### 컨텍스트 및 사용량

[`context_wrapper`][agents.result.RunResultBase.context_wrapper]는 앱 컨텍스트와 함께 승인, 사용량, 중첩 `tool_input` 같은 SDK 관리 런타임 메타데이터를 노출합니다.

사용량은 `context_wrapper.usage`에서 추적됩니다. 스트리밍 실행의 경우 스트림 최종 청크가 처리될 때까지 사용량 합계가 지연될 수 있습니다. 전체 래퍼 형태와 영속성 관련 주의사항은 [Context management](context.md)를 참고하세요.