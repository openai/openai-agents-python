---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 다음 두 결과 유형 중 하나를 받습니다.

-   `Runner.run(...)` 또는 `Runner.run_sync(...)`의 [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)`의 [`RunResultStreaming`][agents.result.RunResultStreaming]

두 유형 모두 [`RunResultBase`][agents.result.RunResultBase]에서 상속되며, `final_output`, `new_items`, `last_agent`, `raw_responses`, `to_state()` 같은 공통 결과 인터페이스를 제공합니다.

`RunResultStreaming`에는 [`stream_events()`][agents.result.RunResultStreaming.stream_events], [`current_agent`][agents.result.RunResultStreaming.current_agent], [`is_complete`][agents.result.RunResultStreaming.is_complete], [`cancel(...)`][agents.result.RunResultStreaming.cancel] 같은 스트리밍 전용 제어 기능이 추가됩니다.

## 적절한 결과 인터페이스 선택 {#choose-the-right-result-surface}

대부분의 애플리케이션에는 몇 가지 결과 속성이나 헬퍼만 필요합니다.

| 필요한 항목 | 사용 대상 |
| --- | --- |
| 사용자에게 표시할 최종 답변 | `final_output` |
| 전체 로컬 대화 기록이 포함된 재생 가능한 다음 턴 입력 목록 | `to_input_list()` |
| 에이전트, 도구, 핸드오프 및 승인 메타데이터를 포함하는 풍부한 실행 항목 | `new_items` |
| 일반적으로 다음 사용자 턴을 처리해야 하는 에이전트 | `last_agent` |
| `previous_response_id`을 사용하는 OpenAI Responses API 체이닝 | `last_response_id` |
| 대기 중인 승인 및 재개 가능한 스냅샷 | `interruptions` 및 `to_state()` |
| 현재 중첩된 `Agent.as_tool()` 호출에 관한 메타데이터 | `agent_tool_invocation` |
| 가공되지 않은 모델 호출 또는 가드레일 진단 | `raw_responses` 및 가드레일 결과 배열 |

## 최종 출력 {#final-output}

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 포함됩니다. 다음 중 하나입니다.

-   마지막 에이전트에 `output_type`이 정의되지 않은 경우 `str`
-   마지막 에이전트에 출력 유형이 정의된 경우 `last_agent.output_type` 유형의 객체
-   승인 인터럽션(중단 처리)으로 일시 중지된 경우처럼 최종 출력이 생성되기 전에 실행이 중지된 경우 `None`

!!! note

    `final_output`은 `Any`로 형식이 지정됩니다. 핸드오프에 따라 실행을 완료하는 에이전트가 달라질 수 있으므로 SDK는 가능한 출력 유형 전체를 정적으로 알 수 없습니다.

스트리밍 모드에서는 스트림 처리가 완료될 때까지 `final_output`가 `None`으로 유지됩니다. 이벤트별 흐름은 [스트리밍](streaming.md)을 참조하세요.

## 입력, 다음 턴 기록 및 새 항목 {#input-next-turn-history-and-new-items}

다음 인터페이스는 서로 다른 질문에 답합니다.

| 속성 또는 헬퍼 | 포함 내용 | 적합한 용도 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 이 실행 구간의 기본 입력입니다. 핸드오프 입력 필터가 기록을 다시 작성한 경우 실행이 계속될 때 사용한 필터링된 입력이 반영됩니다. | 이 실행에서 실제로 사용한 입력 감사 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 실행의 입력 항목 뷰입니다. 기본 `mode="preserve_all"`은 `new_items`에서 변환된 기록을 유지하지만, SDK 기본 중첩 핸드오프 기록으로 이미 이동된 정확히 동일한 세션 항목을 두 번째로 추가하지는 않습니다. `mode="normalized"`은 핸드오프 필터링으로 모델 기록이 다시 작성될 때 표준 계속 입력을 우선합니다. | 수동 채팅 루프, 클라이언트 관리형 대화 상태 및 일반 항목 기록 검사 |
| [`new_items`][agents.result.RunResultBase.new_items] | 에이전트, 도구, 핸드오프 및 승인 메타데이터가 포함된 풍부한 [`RunItem`][agents.items.RunItem] 래퍼입니다. | 로그, UI, 감사 및 디버깅 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 실행의 각 모델 호출에서 수집된 가공되지 않은 [`ModelResponse`][agents.items.ModelResponse] 객체입니다. | 제공자 수준 진단 또는 가공되지 않은 응답 검사 |

실제로는 다음과 같이 사용합니다.

-   실행을 일반 입력 항목 형태로 확인하려면 `to_input_list()`을 사용합니다.
-   핸드오프 필터링 또는 중첩 핸드오프 기록 재작성 후 다음 `Runner.run(..., input=...)` 호출에 사용할 표준 로컬 입력이 필요하면 `to_input_list(mode="normalized")`을 사용합니다.
-   SDK에서 기록을 불러오고 저장하도록 하려면 [`session=...`](sessions/index.md)을 사용합니다.
-   `conversation_id` 또는 `previous_response_id`을 사용하여 OpenAI 서버 관리형 상태를 이용하는 경우 일반적으로 `to_input_list()`을 다시 보내지 말고 새 사용자 입력만 전달한 후 저장된 ID를 재사용합니다.
-   로그, UI 또는 감사에 사용할 전체 변환 기록이 필요하면 기본 `to_input_list()` 모드 또는 `new_items`을 사용합니다.

SDK 기본 중첩 핸드오프 기록에서 메시지 항목을 그대로 보존할 때 Sessions, `RunState`, `to_input_list()`은 콘텐츠를 기준으로 중복 제거하지 않고 정확히 소유된 항목을 추적합니다. 별도로 발생한 동일한 메시지는 별개로 유지되며, 이미 소유된 항목만 두 번째로 추가되지 않습니다.

JavaScript SDK와 달리 Python은 실행 중 새로 생성된 모델 형식 항목만 포함하는 별도의 `output` 속성을 제공하지 않습니다. SDK 메타데이터가 필요하면 `new_items`을 사용하고, 가공되지 않은 모델 페이로드가 필요하면 `raw_responses`을 검사합니다.

컴퓨터 도구 항목을 대화 입력으로 다시 제출할 때는 가공되지 않은 Responses 페이로드 형식을 사용합니다. 프리뷰 모델의 `computer_call` 항목은 단일 `action`을 유지하는 반면, `gpt-5.5` 컴퓨터 호출은 배치된 `actions[]`을 유지할 수 있습니다. [`to_input_list()`][agents.result.RunResultBase.to_input_list] 및 [`RunState`][agents.run_state.RunState]은 모델이 생성한 형식을 그대로 유지하므로 해당 항목을 대화 입력으로 수동 재제출하는 작업, 일시 중지 및 재개 흐름, 저장된 대화 기록이 프리뷰 및 GA 컴퓨터 도구 호출 모두에서 계속 작동합니다. 로컬 실행 결과는 계속 `new_items`에서 `computer_call_output` 항목으로 표시됩니다.

### 새 항목 {#new-items}

[`new_items`][agents.result.RunResultBase.new_items]은 실행 중 발생한 작업을 가장 풍부한 형태로 보여 줍니다. 일반적인 항목 유형은 다음과 같습니다.

-   재개된 모델 호출 직전에 `RunState.pending_input`에서 허용된 입력을 나타내는 [`InputItem`][agents.items.InputItem]
-   어시스턴트 메시지를 나타내는 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   추론 항목을 나타내는 [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses 도구 검색 요청과 불러온 도구 검색 결과를 나타내는 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 및 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   도구 호출 및 그 결과를 나타내는 [`ToolCallItem`][agents.items.ToolCallItem] 및 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   승인을 위해 일시 중지된 도구 호출을 나타내는 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   호스티드 MCP 승인 및 도구 카탈로그를 나타내는 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem], [`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem], [`MCPListToolsItem`][agents.items.MCPListToolsItem]
-   핸드오프 요청 및 완료된 전환을 나타내는 [`HandoffCallItem`][agents.items.HandoffCallItem] 및 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

에이전트 연결 관계, 도구 출력, 핸드오프 경계 또는 승인 경계가 필요할 때는 `to_input_list()`보다 `new_items`을 선택합니다.

호스티드 툴 검색을 사용할 때는 모델이 생성한 검색 요청을 확인하려면 `ToolSearchCallItem.raw_item`을 검사하고, 해당 턴에 불러온 네임스페이스, 함수 또는 호스티드 MCP 서버를 확인하려면 `ToolSearchOutputItem.raw_item`을 검사합니다.

프로그래밍 방식 도구 호출을 사용하면 생성된 `program`은 `ToolCallItem`이고, 해당 프로그램이 소유한 일반 하위 도구 호출도 `ToolCallItem` 항목이며, 일치하는 `program_output`은 `ToolCallOutputItem`입니다. 프로그램이 소유한 호스티드 MCP `mcp_approval_request` 및 `mcp_list_tools` 항목은 예외이며, 각각 `MCPApprovalRequestItem` 및 `MCPListToolsItem` 항목이 됩니다.

가공되지 않은 항목은 형식이 지정된 Responses 객체 또는 매핑일 수 있습니다. 특히 프로그램이 소유한 셸 및 패치 적용 호출은 매핑을 사용합니다. 매핑에 안전한 검사 패턴을 사용하세요.

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

프로그램이 소유한 하위 호출의 경우 `caller`의 `type` 필드는 `program`이며, `caller_id`은 상위 프로그램 호출을 식별합니다.

## 대화 계속 또는 재개 {#continue-or-resume-the-conversation}

### 다음 턴 에이전트 {#next-turn-agent}

[`last_agent`][agents.result.RunResultBase.last_agent]에는 마지막으로 실행된 에이전트가 포함됩니다. 핸드오프 후 다음 사용자 턴에 재사용할 에이전트로 적합한 경우가 많습니다.

스트리밍 모드에서는 실행이 진행됨에 따라 [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent]이 업데이트되므로 스트림이 끝나기 전에 핸드오프를 확인할 수 있습니다.

### 인터럽션(중단 처리) 및 실행 상태 {#interruptions-and-run-state}

도구에 승인이 필요한 경우 대기 중인 승인은 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 또는 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. 여기에는 직접 호출된 도구, 핸드오프 후 도달한 도구 또는 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 발생한 승인이 포함될 수 있습니다.

재개 가능한 [`RunState`][agents.run_state.RunState]을 캡처하려면 [`to_state()`][agents.result.RunResult.to_state]을 호출하고, 대기 중인 항목을 승인하거나 거부한 다음 `Runner.run(...)` 또는 `Runner.run_streamed(...)`로 재개합니다.

[`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 출력이 Pydantic 모델 또는 데이터 클래스인 경우 `RunState`은 해당 출력을 structured data로 직렬화합니다. `RunState`은 딕셔너리, 목록, 튜플도 순회하며 해당 컨테이너에서 발견한 Pydantic 모델 또는 데이터 클래스를 변환합니다. 튜플은 JSON 왕복 처리 후 목록으로 복원됩니다. JSON과 호환되지 않는 다른 값은 문자열 표현으로 대체될 수 있으므로 사용자 지정 유형을 직렬화 후에도 정확하게 유지해야 한다면 명시적으로 JSON과 호환되는 데이터를 반환하세요.

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

#### 재개 전 입력 추가 {#add-input-before-resuming}

실행이 일시 중지되거나 완료된 턴 이후 중지된 다음, 완료되지 않은 실행이 다음 모델 호출에 도달하기 전에 새 사용자 입력이 도착하면 [`RunState.add_input()`][agents.run_state.RunState.add_input]을 사용합니다. 문자열은 사용자 메시지가 되며 여러 번 호출하면 삽입 순서가 유지됩니다. 준비된 입력은 직렬화된 `RunState`의 일부이므로 `to_json()` / `from_json()` 및 `to_string()` / `from_string()` 왕복 처리 후에도 유지됩니다.

```python
state = result.to_state()
state.add_input("Also keep the generated report in the project folder.")

for interruption in state.get_interruptions():
    state.approve(interruption)

result = await Runner.run(agent, state)
```

재개 시 러너는 준비된 입력에만 현재 에이전트의 입력 가드레일과 [`RunConfig`][agents.run.RunConfig]의 입력 가드레일을 모두 적용합니다. 클라이언트 관리형 [`Session`][agents.memory.session.Session]이 구성된 경우 러너는 허용된 준비 입력을 영구 [`InputItem`][agents.items.InputItem]으로 변환하고, 세션 쓰기가 완료될 때까지 기다린 후 모델 요청을 전송합니다. 클라이언트 관리형 세션이나 서버 관리형 대화가 없으면 러너는 모델 요청을 전송하기 전에 허용된 준비 입력을 `InputItem`로 변환합니다. 서버 관리형 대화에서는 서버 요청이 입력을 수락할 때까지 입력이 대기 상태로 유지됩니다. 직렬화, 재개 및 재생에 안전한 재시도 전반에서 SDK는 하나의 영구적인 `InputItem` 항목을 유지합니다. 이 SDK 항목 보장은 제공자 전달 보장이 아닙니다. 요청이 제공자에게 도달했을 가능성이 있는 상황에서 재시도 정책이 `RetryDecision(approve_unsafe_replay=True)`을 반환하면 러너가 준비된 입력을 다시 보낼 수 있으며 제공자 측 작업이 반복될 수 있습니다. 성공적으로 허용된 입력은 `new_items`에 `InputItem`으로 표시됩니다. 분리된 복사본을 가져오려면 [`RunState.pending_input`][agents.run_state.RunState.pending_input]을 읽고, 재개하기 전에 준비된 입력을 모두 삭제하려면 [`RunState.clear_pending_input()`][agents.run_state.RunState.clear_pending_input]을 호출합니다.

`RunState.add_input()`은 종료 상태, 남은 모델 턴이 없는 상태, 수락된 모델 응답이 로컬 처리를 기다리는 상태, 대기 중인 도구 결과가 다른 모델 호출 전에 실행을 종료할 수 있는 인터럽션(중단 처리) 상태를 거부합니다. 이러한 경우에는 현재 실행을 완료한 후 새 사용자 턴을 시작하세요.

스트리밍 실행의 경우 먼저 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 소비를 완료한 다음 `result.interruptions`을 검사하고 `result.to_state()`에서 재개합니다. 전체 승인 흐름은 [휴먼인더루프 (HITL)](human_in_the_loop.md)를 참조하세요.

### 서버 관리형 계속 {#server-managed-continuation}

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 실행에서 가장 최근의 모델 응답 ID입니다. OpenAI Responses API 체인을 계속하려면 다음 턴에 이 ID를 `previous_response_id`으로 다시 전달합니다.

이미 `to_input_list()`, `session` 또는 `conversation_id`을 사용하여 대화를 계속하고 있다면 일반적으로 `last_response_id`은 필요하지 않습니다. 여러 단계로 이루어진 실행의 모든 모델 응답이 필요하면 `raw_responses`을 검사합니다.

## 에이전트 도구 메타데이터 {#agent-as-tool-metadata}

중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 결과가 나온 경우 [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]은 이를 둘러싼 `Agent.as_tool()` 호출에 관한 변경 불가능한 메타데이터를 제공합니다.

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

일반적인 최상위 실행에서 `agent_tool_invocation`은 `None`입니다.

이는 중첩된 결과를 후처리하면서 이를 둘러싼 `Agent.as_tool()` 호출의 도구 이름, 호출 ID 또는 가공되지 않은 인수가 필요할 수 있는 `custom_output_extractor` 내부에서 특히 유용합니다. 관련 `Agent.as_tool()` 패턴은 [도구](tools.md)를 참조하세요.

해당 중첩 실행에 대해 파싱된 structured input도 필요한 경우 `context_wrapper.tool_input`을 읽습니다. 이 필드는 [`RunState`][agents.run_state.RunState]이 중첩 도구 입력을 위해 일반적인 방식으로 직렬화하는 필드이며, `agent_tool_invocation`은 현재 중첩 호출의 메타데이터를 결과에 직접 노출합니다.

## 스트리밍 수명 주기 및 진단 {#streaming-lifecycle-and-diagnostics}

[`RunResultStreaming`][agents.result.RunResultStreaming]은 위와 동일한 결과 인터페이스를 상속하지만 다음과 같은 스트리밍 전용 제어 기능이 추가됩니다.

-   의미론적 스트림 이벤트를 소비하는 [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   실행 도중 활성 에이전트를 추적하는 [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   스트리밍 실행이 완전히 종료되었는지 확인하는 [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   실행을 즉시 또는 현재 턴 이후 중지하는 [`cancel(...)`][agents.result.RunResultStreaming.cancel]

비동기 반복기가 종료될 때까지 `stream_events()`을 계속 소비합니다. 해당 반복기가 종료되기 전까지 스트리밍 실행은 완료되지 않으며, 마지막으로 표시되는 토큰이 도착한 후에도 `final_output`, `interruptions`, `raw_responses` 같은 요약 속성과 세션 지속성 부수 효과가 계속 처리 중일 수 있습니다.

`cancel()`을 호출한 경우 취소 및 정리가 올바르게 완료되도록 `stream_events()`을 계속 소비합니다.

Python은 별도의 스트리밍된 `completed` 프로미스나 `error` 속성을 제공하지 않습니다. 실행을 종료시키는 스트리밍 실패는 `stream_events()`에서 발생하며, `is_complete`은 실행이 종료 상태에 도달했는지를 나타냅니다.

### 가공되지 않은 응답 {#raw-responses}

[`raw_responses`][agents.result.RunResultBase.raw_responses]에는 실행 중 수집된 가공되지 않은 모델 응답이 포함됩니다. 여러 단계로 이루어진 실행에서는 핸드오프 또는 반복되는 모델/도구/모델 주기에 걸쳐 둘 이상의 응답이 생성될 수 있습니다.

[`last_response_id`][agents.result.RunResultBase.last_response_id]는 `raw_responses`의 마지막 항목에서 가져온 ID일 뿐입니다.

각 [`ModelResponse`][agents.items.ModelResponse]은 해당 개별 모델 호출에 적용되는 두 가지 진단 정보도 제공합니다.

-   [`request_id`][agents.items.ModelResponse.request_id]는 모델 어댑터와 전송 계층에서 요청 ID를 전파할 때의 전송 요청 ID입니다. 기본 제공 `OpenAIResponsesModel` 및 `OpenAIChatCompletionsModel`는 HTTP 및 SSE 전송 경로에서 사용 가능한 서버 생성 `x-request-id`을 전파합니다. 구성된 엔드포인트가 OpenAI API인 경우 프로덕션에서 `None`이 아닌 값을 기록하면 장애를 OpenAI 지원팀과 연관 지어 조사할 수 있습니다. OpenAI 호환 제공자 또는 프록시의 경우 해당 서비스의 지원 채널을 대신 사용하세요. `OpenAIResponsesWSModel`은 현재 `request_id`을 `None`으로 유지합니다. 서드 파티 어댑터는 요청 ID 전파를 보장하지 않습니다. AnyLLM Chat Completions 어댑터와 `LitellmModel`은 현재 `request_id`을 `None`으로 유지합니다. Agents SDK AnyLLM Responses 어댑터도 전송 요청 ID를 보존하지 않고 제공자 응답을 정규화할 때 `request_id`을 `None`으로 유지할 수 있습니다.
-   [`raw_usage`][agents.items.ModelResponse.raw_usage]는 Agents SDK가 페이로드를 정규화하기 전 제공자의 사용량 페이로드를 캡처한 선택적 JSON 호환 스냅샷입니다. `ModelSettings(preserve_raw_usage=True)`을 사용하여 `raw_usage`을 활성화하세요. [제공자 사용량 페이로드 보존](usage.md#preserving-provider-usage-payloads)을 참조하세요.

`ModelResponse.request_id`과 `ModelResponse.raw_usage`은 각각 `None`일 수 있으므로 이러한 값을 대화 상태가 아닌 선택적 진단 정보로 처리합니다.

### 가드레일 결과 {#guardrail-results}

에이전트 수준 가드레일은 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 및 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results]로 제공됩니다.

도구 가드레일은 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]로 별도 제공됩니다.

이러한 배열은 실행 전반에 걸쳐 누적되므로 의사 결정을 기록하거나, 추가 가드레일 메타데이터를 저장하거나, 실행이 차단된 이유를 디버깅하는 데 유용합니다.

에이전트 수준 출력 가드레일이 종료 함수 도구에서 직접 생성된 최종 출력을 차단할 때는 하나의 수정 규칙이 적용됩니다. 차단된 현재 응답의 경우 `output_guardrail_results`은 거부된 에이전트 출력을 대체하고 페이로드가 포함된 출력 메타데이터를 지우며, `tool_output_guardrail_results`은 페이로드가 포함된 도구 메타데이터를 대체합니다. 이전에 수락된 결과는 변경되지 않습니다. 정제된 출력 가드레일 결과는 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]의 `guardrail_result`로 제공됩니다. 정제된 출력 가드레일 및 도구 출력 가드레일 결과는 스트리밍 결과 상태와 `RunState`을 통해서도 제공됩니다. [출력 가드레일](guardrails.md#output-guardrails)을 참조하세요.

### 컨텍스트 및 사용량 {#context-and-usage}

[`context_wrapper`][agents.result.RunResultBase.context_wrapper]은 승인, 사용량, 중첩된 `tool_input` 같은 SDK 관리형 런타임 메타데이터와 함께 애플리케이션 컨텍스트를 제공합니다.

사용량은 `context_wrapper.usage`에서 추적됩니다. 스트리밍 실행에서는 스트림의 최종 청크 처리가 완료될 때까지 사용량 합계가 지연될 수 있습니다. 전체 래퍼 형식과 지속성 관련 주의 사항은 [컨텍스트 관리](context.md)를 참조하세요.