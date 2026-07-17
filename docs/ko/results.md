---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 다음 두 결과 타입 중 하나를 받습니다.

-   `Runner.run(...)` 또는 `Runner.run_sync(...)`에서 [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)`에서 [`RunResultStreaming`][agents.result.RunResultStreaming]

둘 다 [`RunResultBase`][agents.result.RunResultBase]를 상속하며, `final_output`, `new_items`, `last_agent`, `raw_responses`, `to_state()`와 같은 공통 결과 인터페이스를 제공합니다.

`RunResultStreaming`에는 [`stream_events()`][agents.result.RunResultStreaming.stream_events], [`current_agent`][agents.result.RunResultStreaming.current_agent], [`is_complete`][agents.result.RunResultStreaming.is_complete], [`cancel(...)`][agents.result.RunResultStreaming.cancel]과 같은 스트리밍 전용 제어 기능이 추가됩니다.

## 적절한 결과 인터페이스 선택

대부분의 애플리케이션에는 몇 가지 결과 속성이나 헬퍼만 필요합니다.

| 필요한 항목 | 사용 대상 |
| --- | --- |
| 사용자에게 표시할 최종 답변 | `final_output` |
| 전체 로컬 대화 기록이 포함되어 다음 턴 재실행에 바로 사용할 수 있는 입력 목록 | `to_input_list()` |
| 에이전트, 도구, 핸드오프 및 승인 메타데이터가 포함된 상세 실행 항목 | `new_items` |
| 일반적으로 다음 사용자 턴을 처리해야 하는 에이전트 | `last_agent` |
| `previous_response_id`를 사용하는 OpenAI Responses API 체인 연결 | `last_response_id` |
| 대기 중인 승인 및 재개 가능한 스냅샷 | `interruptions` 및 `to_state()` |
| 현재 중첩된 `Agent.as_tool()` 호출에 관한 메타데이터 | `agent_tool_invocation` |
| 원문 모델 호출 또는 가드레일 진단 | `raw_responses` 및 가드레일 결과 배열 |

## 최종 출력

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 포함됩니다. 다음 중 하나입니다.

-   마지막 에이전트에 `output_type`이 정의되지 않은 경우 `str`
-   마지막 에이전트에 출력 타입이 정의된 경우 `last_agent.output_type` 타입의 객체
-   승인 인터럽션(중단 처리)으로 일시 중지된 경우처럼 최종 출력이 생성되기 전에 실행이 중단된 경우 `None`

!!! note

    `final_output`의 타입은 `Any`입니다. 핸드오프에 따라 실행을 완료하는 에이전트가 달라질 수 있으므로 SDK는 가능한 모든 출력 타입을 정적으로 알 수 없습니다.

스트리밍 모드에서는 스트림 처리가 완료될 때까지 `final_output`이 `None`으로 유지됩니다. 이벤트별 흐름은 [스트리밍](streaming.md)을 참조하세요.

## 입력, 다음 턴 기록 및 새 항목

이 인터페이스들은 서로 다른 질문에 답합니다.

| 속성 또는 헬퍼 | 포함 내용 | 적합한 용도 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 이 실행 구간의 기본 입력입니다. 핸드오프 입력 필터가 기록을 다시 작성한 경우, 실행이 계속될 때 사용한 필터링된 입력이 반영됩니다. | 이 실행에서 실제로 사용한 입력 감사 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 실행을 입력 항목 형태로 보여 줍니다. 기본 `mode="preserve_all"`은 `new_items`에서 변환된 기록을 유지하지만, SDK 기본 중첩 핸드오프 기록으로 이미 이동된 동일한 세션 항목 인스턴스는 두 번째로 추가하지 않습니다. `mode="normalized"`는 핸드오프 필터링이 모델 기록을 다시 작성할 때 표준 연속 입력을 우선합니다. | 수동 채팅 루프, 클라이언트 관리형 대화 상태 및 일반 항목 기록 검사 |
| [`new_items`][agents.result.RunResultBase.new_items] | 에이전트, 도구, 핸드오프 및 승인 메타데이터가 포함된 상세 [`RunItem`][agents.items.RunItem] 래퍼입니다. | 로그, UI, 감사 및 디버깅 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 실행의 각 모델 호출에서 얻은 원문 [`ModelResponse`][agents.items.ModelResponse] 객체입니다. | 제공자 수준 진단 또는 원문 응답 검사 |

실제로는 다음과 같이 사용합니다.

-   실행을 일반 입력 항목 형태로 확인하려면 `to_input_list()`를 사용합니다.
-   핸드오프 필터링이나 중첩 핸드오프 기록 재작성 후 다음 `Runner.run(..., input=...)` 호출에 사용할 표준 로컬 입력이 필요하면 `to_input_list(mode="normalized")`를 사용합니다.
-   SDK가 기록을 로드하고 저장하도록 하려면 [`session=...`](sessions/index.md)을 사용합니다.
-   `conversation_id` 또는 `previous_response_id`를 사용하여 OpenAI 서버 관리형 상태를 이용하는 경우에는 일반적으로 `to_input_list()`를 다시 보내는 대신 새 사용자 입력만 전달하고 저장된 ID를 재사용합니다.
-   로그, UI 또는 감사에 필요한 전체 변환 기록이 필요하면 기본 `to_input_list()` 모드 또는 `new_items`를 사용합니다.

SDK 기본 중첩 핸드오프 기록이 메시지 항목을 그대로 보존하는 경우, 세션, `RunState`, `to_input_list()`는 콘텐츠를 기준으로 중복을 제거하는 대신 정확히 소유된 항목 인스턴스를 추적합니다. 별도로 발생한 동일한 메시지는 별도 항목으로 유지되며, 이미 소유된 항목 인스턴스만 두 번째로 추가되지 않습니다.

JavaScript SDK와 달리 Python은 모델 형식의 델타만을 위한 별도의 `output` 속성을 제공하지 않습니다. SDK 메타데이터가 필요하면 `new_items`를 사용하고, 원문 모델 페이로드가 필요하면 `raw_responses`를 검사하세요.

컴퓨터 도구 재실행은 원문 Responses 페이로드 형식을 따릅니다. 프리뷰 모델의 `computer_call` 항목은 단일 `action`을 유지하는 반면, `gpt-5.5` 컴퓨터 호출은 배치된 `actions[]`를 유지할 수 있습니다. [`to_input_list()`][agents.result.RunResultBase.to_input_list]와 [`RunState`][agents.run_state.RunState]는 모델이 생성한 형식을 그대로 유지하므로 수동 재실행, 일시 중지/재개 흐름 및 저장된 대화 기록이 프리뷰와 GA 컴퓨터 도구 호출 모두에서 계속 작동합니다. 로컬 실행 결과는 계속해서 `new_items`에 `computer_call_output` 항목으로 표시됩니다.

### 새 항목

[`new_items`][agents.result.RunResultBase.new_items]는 실행 중 발생한 일을 가장 상세하게 보여 줍니다. 일반적인 항목 타입은 다음과 같습니다.

-   어시스턴트 메시지용 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   추론 항목용 [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses 도구 검색 요청 및 로드된 도구 검색 결과용 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem]과 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   도구 호출 및 그 결과용 [`ToolCallItem`][agents.items.ToolCallItem]과 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   승인을 위해 일시 중지된 도구 호출용 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   호스티드 MCP 승인 및 도구 카탈로그용 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem], [`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem], [`MCPListToolsItem`][agents.items.MCPListToolsItem]
-   핸드오프 요청 및 완료된 전달용 [`HandoffCallItem`][agents.items.HandoffCallItem]과 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

에이전트 연결 정보, 도구 출력, 핸드오프 경계 또는 승인 경계가 필요할 때는 `to_input_list()` 대신 `new_items`를 선택하세요.

호스티드 도구 검색을 사용하는 경우, 모델이 생성한 검색 요청을 확인하려면 `ToolSearchCallItem.raw_item`을 검사하고 해당 턴에 로드된 네임스페이스, 함수 또는 호스티드 MCP 서버를 확인하려면 `ToolSearchOutputItem.raw_item`을 검사하세요.

Programmatic Tool Calling을 사용하면 생성된 `program`은 `ToolCallItem`이고, 해당 프로그램이 소유한 일반 하위 도구 호출도 `ToolCallItem` 항목이며, 이에 대응하는 `program_output`은 `ToolCallOutputItem`입니다. 프로그램 소유의 호스티드 MCP `mcp_approval_request` 및 `mcp_list_tools` 항목은 예외로, 각각 `MCPApprovalRequestItem` 및 `MCPListToolsItem` 항목이 됩니다.

원문 항목은 타입이 지정된 Responses 객체이거나 매핑일 수 있습니다. 특히 프로그램 소유의 셸 및 패치 적용 호출은 매핑을 사용합니다. 매핑에도 안전한 검사 패턴을 사용하세요.

```python
from collections.abc import Mapping


def raw_field(item, name):
    raw_item = item.raw_item
    if isinstance(raw_item, Mapping):
        return raw_item.get(name)
    return getattr(raw_item, name, None)


raw_type = raw_field(item, "type")
caller = raw_field(item, "caller")
caller_id = (
    caller.get("caller_id")
    if isinstance(caller, Mapping)
    else getattr(caller, "caller_id", None)
)
```

프로그램 소유의 하위 호출에서 `caller`의 타입은 `program`이며, `caller_id`는 상위 프로그램 호출을 식별합니다.

## 대화 계속 또는 재개

### 다음 턴 에이전트

[`last_agent`][agents.result.RunResultBase.last_agent]에는 마지막으로 실행된 에이전트가 포함됩니다. 핸드오프 후 다음 사용자 턴에 재사용하기에 가장 적합한 에이전트인 경우가 많습니다.

스트리밍 모드에서는 실행이 진행됨에 따라 [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent]가 업데이트되므로 스트림이 완료되기 전에 핸드오프를 관찰할 수 있습니다.

### 인터럽션(중단 처리) 및 실행 상태

도구에 승인이 필요한 경우, 대기 중인 승인은 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 또는 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. 여기에는 직접 호출된 도구, 핸드오프 후 도달한 도구 또는 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 발생한 승인이 포함될 수 있습니다.

[`to_state()`][agents.result.RunResult.to_state]를 호출하여 재개 가능한 [`RunState`][agents.run_state.RunState]를 캡처하고, 대기 중인 항목을 승인하거나 거부한 다음 `Runner.run(...)` 또는 `Runner.run_streamed(...)`으로 재개합니다.

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

스트리밍 실행에서는 먼저 [`stream_events()`][agents.result.RunResultStreaming.stream_events]를 끝까지 소비한 다음 `result.interruptions`를 검사하고 `result.to_state()`에서 재개합니다. 전체 승인 흐름은 [휴먼인더루프 (HITL)](human_in_the_loop.md)를 참조하세요.

### 서버 관리형 연속 실행

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 실행에서 얻은 최신 모델 응답 ID입니다. OpenAI Responses API 체인을 계속하려면 다음 턴에 이를 `previous_response_id`로 다시 전달합니다.

이미 `to_input_list()`, `session` 또는 `conversation_id`를 사용하여 대화를 계속하고 있다면 일반적으로 `last_response_id`가 필요하지 않습니다. 여러 단계 실행의 모든 모델 응답이 필요하면 대신 `raw_responses`를 검사하세요.

## 도구로 사용되는 에이전트의 메타데이터

중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 결과가 나온 경우, [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]은 외부 도구 호출에 관한 변경 불가능한 메타데이터를 제공합니다.

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

일반적인 최상위 실행에서 `agent_tool_invocation`은 `None`입니다.

이는 중첩된 결과를 후처리하면서 외부 도구 이름, 호출 ID 또는 원문 인수가 필요할 수 있는 `custom_output_extractor` 내부에서 특히 유용합니다. 관련 `Agent.as_tool()` 패턴은 [도구](tools.md)를 참조하세요.

해당 중첩 실행에 대해 파싱된 구조화 입력도 필요하면 `context_wrapper.tool_input`을 읽으세요. 이는 [`RunState`][agents.run_state.RunState]가 중첩 도구 입력을 범용 방식으로 직렬화하는 필드이며, `agent_tool_invocation`은 현재 중첩 호출을 위한 실시간 결과 접근자입니다.

## 스트리밍 수명 주기 및 진단

[`RunResultStreaming`][agents.result.RunResultStreaming]은 위와 동일한 결과 인터페이스를 상속하면서 다음과 같은 스트리밍 전용 제어 기능을 추가합니다.

-   의미론적 스트림 이벤트를 소비하는 [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   실행 중 활성 에이전트를 추적하는 [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   스트리밍 실행이 완전히 완료되었는지 확인하는 [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   실행을 즉시 또는 현재 턴 이후에 중지하는 [`cancel(...)`][agents.result.RunResultStreaming.cancel]

비동기 이터레이터가 끝날 때까지 `stream_events()`를 계속 소비하세요. 이 이터레이터가 종료되기 전에는 스트리밍 실행이 완료된 것이 아니며, 마지막으로 표시되는 토큰이 도착한 후에도 `final_output`, `interruptions`, `raw_responses` 같은 요약 속성과 세션 영속화 부수 효과가 아직 처리 중일 수 있습니다.

`cancel()`을 호출한 경우에도 취소 및 정리가 올바르게 완료될 수 있도록 `stream_events()`를 계속 소비하세요.

Python은 스트리밍용으로 별도의 `completed` 프로미스나 `error` 속성을 제공하지 않습니다. 스트리밍을 종료시키는 오류는 `stream_events()`에서 예외를 발생시키는 방식으로 노출되며, `is_complete`는 실행이 종료 상태에 도달했는지를 나타냅니다.

### 원문 응답

[`raw_responses`][agents.result.RunResultBase.raw_responses]에는 실행 중 수집된 원문 모델 응답이 포함됩니다. 여러 단계 실행에서는 핸드오프나 반복되는 모델/도구/모델 주기 등으로 인해 둘 이상의 응답이 생성될 수 있습니다.

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 `raw_responses`의 마지막 항목에 있는 ID일 뿐입니다.

### 가드레일 결과

에이전트 수준 가드레일은 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 및 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results]로 노출됩니다.

도구 가드레일은 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]로 별도 노출됩니다.

이 배열들은 실행 전반에 걸쳐 누적되므로 판단 기록, 추가 가드레일 메타데이터 저장 또는 실행이 차단된 이유를 디버깅하는 데 유용합니다.

### 컨텍스트 및 사용량

[`context_wrapper`][agents.result.RunResultBase.context_wrapper]는 승인, 사용량, 중첩된 `tool_input`과 같은 SDK 관리형 런타임 메타데이터와 함께 앱 컨텍스트를 제공합니다.

사용량은 `context_wrapper.usage`에서 추적됩니다. 스트리밍 실행에서는 스트림의 마지막 청크가 처리될 때까지 사용량 합계 반영이 늦어질 수 있습니다. 전체 래퍼 구조 및 영속성 관련 주의 사항은 [컨텍스트 관리](context.md)를 참조하세요.