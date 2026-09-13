---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 다음 두 결과 유형 중 하나를 받습니다.

-   `Runner.run(...)` 또는 `Runner.run_sync(...)`에서 반환되는 [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)`에서 반환되는 [`RunResultStreaming`][agents.result.RunResultStreaming]

둘 다 [`RunResultBase`][agents.result.RunResultBase]을 상속하며, `final_output`, `new_items`, `last_agent`, `raw_responses`, `to_state()` 같은 공통 결과 인터페이스를 제공합니다.

`RunResultStreaming`에는 [`stream_events()`][agents.result.RunResultStreaming.stream_events], [`current_agent`][agents.result.RunResultStreaming.current_agent], [`is_complete`][agents.result.RunResultStreaming.is_complete], [`cancel(...)`][agents.result.RunResultStreaming.cancel] 같은 스트리밍 전용 제어 기능이 추가됩니다.

## 적절한 결과 인터페이스 {#choose-the-right-result-surface}

대부분의 애플리케이션에는 몇 가지 결과 속성이나 헬퍼만 필요합니다.

| 필요한 항목 | 사용 대상 |
| --- | --- |
| 사용자에게 표시할 최종 답변 | `final_output` |
| 전체 로컬 트랜스크립트를 포함하며 재실행할 수 있는 다음 턴 입력 목록 | `to_input_list()` |
| 에이전트, 도구, 핸드오프 및 승인 메타데이터가 포함된 풍부한 실행 항목 | `new_items` |
| 일반적으로 다음 사용자 턴을 처리해야 하는 에이전트 | `last_agent` |
| `previous_response_id`을 사용한 OpenAI Responses API 체이닝 | `last_response_id` |
| 대기 중인 승인 및 재개 가능한 스냅샷 | `interruptions` 및 `to_state()` |
| 현재 중첩된 `Agent.as_tool()` 호출에 대한 메타데이터 | `agent_tool_invocation` |
| raw 모델 호출 또는 가드레일 진단 | `raw_responses` 및 가드레일 결과 배열 |

## 최종 출력 {#final-output}

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 포함됩니다. 다음 중 하나입니다.

-   마지막 에이전트에 `output_type`이 정의되지 않은 경우 `str`
-   마지막 에이전트에 출력 유형이 정의된 경우 `last_agent.output_type` 유형의 객체
-   승인 인터럽션(중단 처리)으로 일시 중지된 경우처럼 최종 출력이 생성되기 전에 실행이 중지된 경우 `None`

!!! note

    `final_output`의 유형은 `Any`입니다. 핸드오프로 인해 어떤 에이전트가 실행을 완료할지 달라질 수 있으므로 SDK는 가능한 모든 출력 유형을 정적으로 파악할 수 없습니다.

스트리밍 모드에서 `final_output`은 스트림 처리가 완료될 때까지 `None` 상태로 유지됩니다. 이벤트별 흐름은 [스트리밍](streaming.md)을 참고하세요.

## 입력, 다음 턴 기록 및 새 항목 {#input-next-turn-history-and-new-items}

다음 인터페이스는 서로 다른 질문에 답합니다.

| 속성 또는 헬퍼 | 포함 내용 | 적합한 용도 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 이 실행 구간의 기본 입력입니다. 핸드오프 입력 필터가 기록을 다시 작성했다면 실행이 계속된 필터링된 입력을 반영합니다. | 이 실행에서 실제로 사용한 입력 감사 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 실행의 입력 항목 뷰입니다. 기본값 `mode="preserve_all"`은 `new_items`에서 변환된 기록을 유지하지만, SDK 기본 중첩 핸드오프 기록으로 이미 이동한 정확히 동일한 세션 항목 발생을 다시 추가하지는 않습니다. 핸드오프 필터링이 모델 기록을 다시 작성하는 경우 `mode="normalized"`은 정규 연속 입력을 우선합니다. | 수동 채팅 루프, 클라이언트 관리형 대화 상태 및 일반 항목 기록 검사 |
| [`new_items`][agents.result.RunResultBase.new_items] | 에이전트, 도구, 핸드오프 및 승인 메타데이터가 포함된 풍부한 [`RunItem`][agents.items.RunItem] 래퍼입니다. | 로그, UI, 감사 및 디버깅 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 실행 중 각 모델 호출에서 반환된 raw [`ModelResponse`][agents.items.ModelResponse] 객체입니다. | 제공자 수준 진단 또는 raw 응답 검사 |

실제로는 다음과 같이 사용합니다.

-   실행을 일반 입력 항목 형태로 확인하려면 `to_input_list()`을 사용합니다.
-   핸드오프 필터링이나 중첩 핸드오프 기록 재작성 후 다음 `Runner.run(..., input=...)` 호출에 사용할 정규 로컬 입력이 필요하면 `to_input_list(mode="normalized")`을 사용합니다.
-   SDK가 기록을 자동으로 불러오고 저장하게 하려면 [`session=...`](sessions/index.md)을 사용합니다.
-   `conversation_id` 또는 `previous_response_id`을 사용해 OpenAI 서버 관리형 상태를 이용하는 경우, 일반적으로 `to_input_list()`을 다시 전송하지 말고 새 사용자 입력만 전달하면서 저장된 ID를 재사용합니다.
-   로그, UI 또는 감사에 변환된 전체 기록이 필요하면 기본 `to_input_list()` 모드나 `new_items`을 사용합니다.

SDK 기본 중첩 핸드오프 기록에서 메시지 항목을 그대로 보존하는 경우 Sessions, `RunState`, `to_input_list()`은 콘텐츠를 기준으로 중복 제거하지 않고 소유한 정확한 항목 발생을 추적합니다. 별도로 발생한 동일한 메시지는 계속 별개로 유지되며, 이미 소유한 항목 발생만 두 번째로 추가되지 않습니다.

모델 출력을 재실행 가능한 입력으로 변환할 때 `to_input_list()`, [`ModelResponse.to_input_items()`][agents.items.ModelResponse.to_input_items], 각 [`RunItemBase.to_input_item()`][agents.items.RunItemBase.to_input_item] 호출은 제공자의 출력 전용 `created_by` 메타데이터를 제거합니다. 여기에는 중첩된 `shell_call_output` 청크의 `created_by`도 포함됩니다. 변환 과정에서는 영향을 받는 매핑을 다시 구성하며 원래 raw 항목을 변경하지 않습니다.

JavaScript SDK와 달리 Python은 실행 중 새로 생성된 모델 형식 항목만 포함하는 별도의 `output` 속성을 제공하지 않습니다. SDK 메타데이터가 필요하면 `new_items`을 사용하고, raw 모델 페이로드가 필요하면 `raw_responses`을 검사합니다.

컴퓨터 도구 항목을 대화 입력으로 다시 제출할 때는 raw Responses 페이로드 형식을 사용합니다. 프리뷰 모델의 `computer_call` 항목은 단일 `action`을 보존하지만, `gpt-5.5` 컴퓨터 호출은 배치된 `actions[]`을 보존할 수 있습니다. [`to_input_list()`][agents.result.RunResultBase.to_input_list] 및 [`RunState`][agents.run_state.RunState]은 모델이 생성한 형식을 그대로 유지하므로, 해당 항목을 대화 입력으로 수동 재제출하는 작업, 일시 중지 및 재개 흐름, 저장된 트랜스크립트가 프리뷰와 GA 컴퓨터 도구 호출 모두에서 계속 작동합니다. 로컬 실행 결과는 `new_items`에서 계속 `computer_call_output` 항목으로 표시됩니다.

### 새 항목 {#new-items}

[`new_items`][agents.result.RunResultBase.new_items]은 실행 중 발생한 작업을 가장 상세하게 보여 줍니다. 일반적인 항목 유형은 다음과 같습니다.

-   재개된 모델 호출 직전에 `RunState.pending_input`에서 수용한 입력을 나타내는 [`InputItem`][agents.items.InputItem]
-   어시스턴트 메시지를 나타내는 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   추론 항목을 나타내는 [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses 도구 검색 요청과 불러온 도구 검색 결과를 나타내는 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 및 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   도구 호출과 그 결과를 나타내는 [`ToolCallItem`][agents.items.ToolCallItem] 및 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   승인을 위해 일시 중지된 도구 호출을 나타내는 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   호스티드 MCP 승인과 도구 카탈로그를 나타내는 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem], [`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem], [`MCPListToolsItem`][agents.items.MCPListToolsItem]
-   핸드오프 요청과 완료된 전환을 나타내는 [`HandoffCallItem`][agents.items.HandoffCallItem] 및 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

에이전트 연결 관계, 도구 출력, 핸드오프 경계 또는 승인 경계가 필요할 때는 항상 `to_input_list()`보다 `new_items`을 선택합니다.

호스티드 툴 검색을 사용할 때는 모델이 생성한 검색 요청을 확인하려면 `ToolSearchCallItem.raw_item`을 검사하고, 해당 턴에 불러온 네임스페이스, 함수 또는 호스티드 MCP 서버를 확인하려면 `ToolSearchOutputItem.raw_item`을 검사합니다.

프로그래밍 방식 도구 호출을 사용하면 생성된 `program`은 `ToolCallItem`이고, 해당 프로그램이 소유한 일반 하위 도구 호출도 `ToolCallItem` 항목이며, 일치하는 `program_output`은 `ToolCallOutputItem`입니다. 프로그램이 소유한 호스티드 MCP `mcp_approval_request` 및 `mcp_list_tools` 항목은 예외로, `MCPApprovalRequestItem` 및 `MCPListToolsItem` 항목이 됩니다.

raw 항목은 형식이 지정된 Responses 객체 또는 매핑일 수 있습니다. 특히 프로그램이 소유한 셸 및 패치 적용 호출은 매핑을 사용합니다. 다음과 같이 매핑을 안전하게 검사하는 패턴을 사용합니다.

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

프로그램이 소유한 하위 호출에서는 `caller`의 `type` 필드가 `program`이며, `caller_id`은 상위 프로그램 호출을 식별합니다.

## 대화의 계속 및 재개 {#continue-or-resume-the-conversation}

### 다음 턴 에이전트 {#next-turn-agent}

[`last_agent`][agents.result.RunResultBase.last_agent]에는 마지막으로 실행된 에이전트가 포함됩니다. 핸드오프 후 다음 사용자 턴에 재사용하기에 가장 적합한 에이전트인 경우가 많습니다.

스트리밍 모드에서는 실행이 진행되는 동안 [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent]이 업데이트되므로 스트림이 완료되기 전에 핸드오프를 확인할 수 있습니다.

### 인터럽션(중단 처리) 및 실행 상태 {#interruptions-and-run-state}

도구에 승인이 필요한 경우 대기 중인 승인은 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 또는 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]에 노출됩니다. 여기에는 직접 호출된 도구, 핸드오프 후 도달한 도구 또는 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 발생한 승인이 포함될 수 있습니다.

[`to_state()`][agents.result.RunResult.to_state]을 호출하여 재개 가능한 [`RunState`][agents.run_state.RunState]을 캡처하고 대기 중인 항목을 승인하거나 거부한 다음, `Runner.run(...)` 또는 `Runner.run_streamed(...)`으로 재개합니다.

[`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 출력이 Pydantic 모델 또는 데이터 클래스인 경우 `RunState`은 해당 출력을 structured outputs로 직렬화합니다. `RunState`은 딕셔너리, 목록 및 튜플도 순회하며 해당 컨테이너에서 발견한 Pydantic 모델이나 데이터 클래스를 변환합니다. 튜플은 JSON 왕복 처리 후 목록으로 복원됩니다. JSON과 호환되지 않는 다른 값은 문자열 표현으로 대체될 수 있으므로, 정확한 사용자 지정 유형이 직렬화 후에도 유지되어야 한다면 명시적으로 JSON 호환 데이터를 반환합니다.

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

#### 재개된 세션 쓰기 실패 복구 {#recover-a-failed-resumed-session-write}

재개된 실행은 승인된 도구 작업을 완료한 후에도 같은 모델 응답에서의 핸드오프를 포함하여 또 다른 모델 호출로 이어질 수 있으며, 이후 완료된 도구 호출과 출력을 클라이언트 관리형 [`Session`][agents.memory.session.Session]에 쓰는 동안 실패할 수 있습니다. 동일한 [`RunState`][agents.run_state.RunState]을 유지하거나 직렬화 후 복원한 다음, 원래 세션 백엔드 및 `session_id`과 함께 `Runner.run(...)` 또는 `Runner.run_streamed(...)`을 다시 시도합니다. 이후 모델 호출 전에 SDK는 대기 중인 배치를 세션 기록과 조정합니다. 세션이 전체 배치를 커밋했지만 확인 응답에 실패한 경우 SDK는 정확히 일치하는 기록의 끝부분을 인식하고 배치를 다시 추가하지 않습니다. 쓰기가 커밋되지 않았다면 SDK는 추가 작업을 다시 시도합니다. SDK는 완료된 도구, 도구 가드레일, 훅 또는 핸드오프를 다시 실행하지 않습니다. 재개된 실행은 완료된 핸드오프에서 선택된 에이전트로 계속됩니다.

세션 기록이 정확히 일치하지 않으면 복구는 안전하게 실패합니다. 원래 세션 백엔드와 `session_id`을 사용하고 재개된 실행이 해당 기록에 독점적으로 접근하도록 합니다. 다른 작성자가 기록의 끝부분을 변경하거나 대기 중인 배치 중 일부만 존재하거나 기록이 그 밖의 이유로 모호한 경우, SDK는 다른 모델을 호출하기 전에 [`UserError`][agents.exceptions.UserError]을 발생시킵니다. 재개하기 전에 원래 세션 기록을 복구하고, 완료된 작업을 다시 실행하지 마세요. 대기 중인 배치, 선택된 에이전트 및 누적된 도구 가드레일 결과는 복구가 완료되기 전 나중에 발생한 승인 인터럽션(중단 처리)을 포함하여 `RunState`의 JSON 및 문자열 왕복 처리 후에도 유지됩니다. [`stream_events()`][agents.result.RunResultStreaming.stream_events]이 세션 쓰기 오류를 발생시킨 후에도 [`RunResultStreaming.to_state()`][agents.result.RunResultStreaming.to_state]은 동일한 복구 데이터를 유지하는 분리된 상태를 반환합니다.

실행이 최종 출력을 수용하고 출력 가드레일과 종료 훅을 완료한 후 해당 최종 턴을 유지하는 데 실패한 경우에는 이 복구가 적용되지 않습니다. 해당 상태를 재실행하면 종료 수명 주기 효과가 반복될 수 있으므로 SDK는 `RunState`을 복구 불가능으로 표시합니다. 이후 해당 상태를 사용하는 모든 `Runner.run(...)` 또는 `Runner.run_streamed(...)` 시도는 세션 조정, 샌드박스 준비, 모델 호출, 도구, 가드레일 또는 훅 전에 [`UserError`][agents.exceptions.UserError]을 발생시킵니다. 이 표시는 `RunState` 직렬화 후에도 유지됩니다. 해당 상태를 다시 시도하는 대신 새 실행을 시작합니다. 이 경계는 종료 함수 도구 출력과 기록에 수용된 `max_turns` 핸들러 출력에도 적용됩니다.

#### 재개 전 입력 추가 {#add-input-before-resuming}

실행이 일시 중지되거나 완료된 턴 후 중지되었지만 완료되지 않은 실행이 다음 모델 호출에 도달하기 전에 새 사용자 입력이 도착하면 [`RunState.add_input()`][agents.run_state.RunState.add_input]을 사용합니다. 문자열은 사용자 메시지가 되며 여러 번 호출해도 삽입 순서가 유지됩니다. 준비된 입력은 직렬화된 `RunState`의 일부이므로 `to_json()` / `from_json()` 및 `to_string()` / `from_string()` 왕복 처리 후에도 유지됩니다.

```python
state = result.to_state()
state.add_input("Also keep the generated report in the project folder.")

for interruption in state.get_interruptions():
    state.approve(interruption)

result = await Runner.run(agent, state)
```

재개 시 러너는 현재 에이전트의 입력 가드레일과 [`RunConfig`][agents.run.RunConfig]의 입력 가드레일을 준비된 입력에만 적용합니다. 클라이언트 관리형 [`Session`][agents.memory.session.Session]이 구성된 경우 러너는 수용된 준비 입력을 영구 [`InputItem`][agents.items.InputItem]으로 변환하고 모델 요청을 보내기 전에 세션 쓰기가 완료되기를 기다립니다. 클라이언트 관리형 세션이나 서버 관리형 대화가 없으면 러너는 모델 요청을 보내기 전에 수용된 준비 입력을 `InputItem`으로 변환합니다. 서버 관리형 대화의 경우 서버 요청에서 입력을 수용할 때까지 해당 입력은 대기 상태로 유지됩니다. 직렬화, 재개 및 재실행에 안전한 재시도 전반에서 SDK는 하나의 영구 `InputItem` 발생을 보존합니다. 이 SDK 항목 발생 보장은 제공자 전달을 보장하지 않습니다. 요청이 제공자에게 도달했을 가능성이 있는 상태에서 재시도 정책이 `RetryDecision(approve_unsafe_replay=True)`을 반환하면 러너가 준비된 입력을 다시 전송하여 제공자 측 작업이 반복될 수 있습니다. 정상적으로 수용된 입력은 `new_items`에 `InputItem`으로 표시됩니다. 분리된 복사본을 확인하려면 [`RunState.pending_input`][agents.run_state.RunState.pending_input]을 읽고, 재개 전에 준비된 입력을 모두 삭제하려면 [`RunState.clear_pending_input()`][agents.run_state.RunState.clear_pending_input]을 호출합니다.

`RunState.add_input()`은 종료 상태, 남은 모델 턴이 없는 상태, 수용된 모델 응답이 로컬 처리를 기다리는 상태, 대기 중인 도구 결과가 다른 모델 호출 전에 실행을 종료할 수 있는 인터럽션(중단 처리) 상태를 거부합니다. 이러한 경우에는 현재 실행을 완료하고 새 사용자 턴을 시작합니다.

스트리밍 실행의 경우 먼저 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 소비를 완료한 다음 `result.interruptions`을 검사하고 `result.to_state()`에서 재개합니다. 전체 승인 흐름은 [휴먼인더루프 (HITL)](human_in_the_loop.md)를 참고하세요.

### 서버 관리형 연속 실행 {#server-managed-continuation}

[`last_response_id`][agents.result.RunResultBase.last_response_id]은 실행의 최신 모델 응답 ID입니다. OpenAI Responses API 체인을 계속하려면 다음 턴에 `previous_response_id`으로 다시 전달합니다.

이미 `to_input_list()`, `session` 또는 `conversation_id`을 사용해 대화를 계속하고 있다면 일반적으로 `last_response_id`은 필요하지 않습니다. 여러 단계로 이루어진 실행의 모든 모델 응답이 필요하다면 대신 `raw_responses`을 검사합니다.

## 에이전트-도구 메타데이터 {#agent-as-tool-metadata}

중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에서 결과가 반환되면 [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]은 이를 둘러싼 `Agent.as_tool()` 호출에 대한 불변 메타데이터를 제공합니다.

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

일반적인 최상위 실행에서 `agent_tool_invocation`은 `None`입니다.

이는 `custom_output_extractor` 내부에서 특히 유용합니다. 중첩된 결과를 후처리하는 동안 이를 둘러싼 `Agent.as_tool()` 호출의 도구 이름, 호출 ID 또는 raw 인수가 필요할 수 있기 때문입니다. 관련 `Agent.as_tool()` 패턴은 [도구](tools.md)를 참고하세요.

해당 중첩 실행의 파싱된 structured outputs도 필요하다면 `context_wrapper.tool_input`을 읽습니다. 이는 [`RunState`][agents.run_state.RunState]이 중첩된 도구 입력을 위해 일반적으로 직렬화하는 필드이며, `agent_tool_invocation`은 현재 중첩 호출의 메타데이터를 결과에 직접 제공합니다.

## 스트리밍 수명 주기 및 진단 {#streaming-lifecycle-and-diagnostics}

[`RunResultStreaming`][agents.result.RunResultStreaming]은 위와 같은 결과 인터페이스를 상속하지만 다음과 같은 스트리밍 전용 제어 기능도 추가합니다.

-   의미론적 스트림 이벤트를 소비하는 [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   실행 중 활성 에이전트를 추적하는 [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   스트리밍 실행이 완전히 끝났는지 확인하는 [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   실행을 즉시 또는 현재 턴 이후에 중지하는 [`cancel(...)`][agents.result.RunResultStreaming.cancel]

비동기 이터레이터가 완료될 때까지 `stream_events()`을 계속 소비합니다. 해당 이터레이터가 끝날 때까지 스트리밍 실행은 완료된 것이 아니며, 마지막으로 표시되는 토큰이 도착한 후에도 `final_output`, `interruptions`, `raw_responses` 같은 요약 속성과 세션 유지 부수 효과가 아직 처리 중일 수 있습니다.

`cancel()`을 호출한 경우 취소 및 정리가 올바르게 완료되도록 `stream_events()`을 계속 소비합니다.

Python은 별도의 스트리밍된 `completed` 프로미스나 `error` 속성을 제공하지 않습니다. 실행을 종료시키는 스트리밍 실패는 `stream_events()`에서 발생하며, `is_complete`은 실행이 종료 상태에 도달했는지를 나타냅니다.

### Raw 응답 {#raw-responses}

[`raw_responses`][agents.result.RunResultBase.raw_responses]에는 실행 중 수집된 raw 모델 응답이 포함됩니다. 여러 단계로 이루어진 실행에서는 핸드오프나 반복되는 모델/도구/모델 주기 등으로 인해 둘 이상의 응답이 생성될 수 있습니다.

[`last_response_id`][agents.result.RunResultBase.last_response_id]은 `raw_responses`의 마지막 항목에 있는 ID일 뿐입니다.

각 [`ModelResponse`][agents.items.ModelResponse]은 개별 모델 호출에 적용되는 다음 두 진단 정보도 제공합니다.

-   [`request_id`][agents.items.ModelResponse.request_id]은 모델 어댑터와 전송 계층이 요청 ID를 전달하는 경우의 전송 요청 ID입니다. 기본 제공 `OpenAIResponsesModel` 및 `OpenAIChatCompletionsModel`은 HTTP와 SSE 전송 경로에서 사용 가능한 서버 생성 `x-request-id`을 전달합니다. 구성된 엔드포인트가 OpenAI API인 경우 프로덕션 환경에서 `None`이 아닌 값을 기록하면 장애를 OpenAI 지원팀과 연관 지어 확인할 수 있습니다. OpenAI 호환 제공자나 프록시의 경우에는 해당 서비스의 지원 채널을 사용합니다. `OpenAIResponsesWSModel`은 현재 `request_id`을 `None` 상태로 둡니다. 서드 파티 어댑터는 요청 ID 전달을 보장하지 않습니다. AnyLLM Chat Completions 어댑터와 `LitellmModel`은 현재 `request_id`을 `None` 상태로 둡니다. Agents SDK AnyLLM Responses 어댑터도 전송 요청 ID를 보존하지 않고 제공자 응답을 정규화하는 경우 `request_id`을 `None` 상태로 둘 수 있습니다.
-   [`raw_usage`][agents.items.ModelResponse.raw_usage]은 Agents SDK가 제공자의 사용량 페이로드를 정규화하기 전 상태를 담은 선택적 JSON 호환 스냅샷입니다. `ModelSettings(preserve_raw_usage=True)`을 사용해 `raw_usage`을 활성화합니다. [제공자 사용량 페이로드 보존](usage.md#preserving-provider-usage-payloads)을 참고하세요.

`ModelResponse.request_id`과 `ModelResponse.raw_usage`은 각각 `None`일 수 있으므로 이러한 값을 대화 상태가 아닌 선택적 진단 정보로 처리합니다.

### 가드레일 결과 {#guardrail-results}

에이전트 수준 가드레일은 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 및 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results]에 노출됩니다.

도구 가드레일은 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]에 별도로 노출됩니다.

이러한 배열은 실행 전반에 걸쳐 누적되므로 판단을 기록하거나 추가 가드레일 메타데이터를 저장하거나 실행이 차단된 이유를 디버깅하는 데 유용합니다.

에이전트 수준 출력 가드레일이 종료 함수 도구에서 직접 생성된 최종 출력을 차단할 때는 하나의 수정 규칙이 적용됩니다. 차단된 현재 응답에서 `output_guardrail_results`은 거부된 에이전트 출력을 대체하고 페이로드가 포함된 출력 메타데이터를 삭제하며, `tool_output_guardrail_results`은 페이로드가 포함된 도구 메타데이터를 대체합니다. 이전에 수용된 결과는 변경되지 않습니다. 정제된 출력 가드레일 결과는 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]의 `guardrail_result`으로 제공됩니다. 정제된 출력 가드레일 및 도구 출력 가드레일 결과는 스트리밍된 결과 상태와 `RunState`을 통해서도 제공됩니다. [출력 가드레일](guardrails.md#output-guardrails)을 참고하세요.

### 컨텍스트 및 사용량 {#context-and-usage}

[`context_wrapper`][agents.result.RunResultBase.context_wrapper]은 승인, 사용량, 중첩된 `tool_input` 같은 SDK 관리형 런타임 메타데이터와 함께 애플리케이션 컨텍스트를 제공합니다.

사용량은 `context_wrapper.usage`에서 추적됩니다. 스트리밍 실행에서는 스트림의 마지막 청크 처리가 완료될 때까지 사용량 합계 반영이 지연될 수 있습니다. 전체 래퍼 형식과 유지 관련 주의 사항은 [컨텍스트 관리](context.md)를 참고하세요.