---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 다음 중 하나를 받게 됩니다:

- `run` 또는 `run_sync`를 호출하는 경우 [`RunResult`][agents.result.RunResult]
- `run_streamed`를 호출하는 경우 [`RunResultStreaming`][agents.result.RunResultStreaming]

이 둘은 모두 [`RunResultBase`][agents.result.RunResultBase]를 상속하며, 대부분의 유용한 정보가 여기에 포함되어 있습니다.

## 최종 출력

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 들어 있습니다. 이는 다음 중 하나입니다:

- 마지막 에이전트에 `output_type`이 정의되어 있지 않다면 `str`
- 에이전트에 출력 타입이 정의되어 있다면 `last_agent.output_type` 타입의 객체

!!! note

    `final_output`의 타입은 `Any`입니다. 핸드오프 때문에 이를 정적으로 타입 지정할 수 없습니다. 핸드오프가 발생하면 어떤 Agent든 마지막 에이전트가 될 수 있으므로, 가능한 출력 타입의 집합을 정적으로 알 수 없습니다.

## 다음 턴의 입력

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list]를 사용하면 결과를 입력 리스트로 변환할 수 있으며, 여기에는 사용자가 제공한 원래 입력에 더해 에이전트 실행 중 생성된 항목들이 이어 붙여집니다. 이를 통해 한 번의 에이전트 실행 출력들을 다음 실행의 입력으로 전달하거나, 루프로 실행하면서 매번 새로운 사용자 입력을 추가하기가 편리합니다.

## 마지막 에이전트

[`last_agent`][agents.result.RunResultBase.last_agent] 속성에는 마지막으로 실행된 에이전트가 들어 있습니다. 애플리케이션에 따라, 사용자가 다음에 무언가를 입력할 때 이를 활용하는 경우가 많습니다. 예를 들어, 최전선 트리아지 에이전트가 언어별 에이전트로 핸드오프하는 구조라면, 마지막 에이전트를 저장해 두었다가 사용자가 다음에 에이전트에게 메시지를 보낼 때 재사용할 수 있습니다.

## 새 항목

[`new_items`][agents.result.RunResultBase.new_items] 속성에는 실행 중 생성된 새 항목들이 들어 있습니다. 항목들은 [`RunItem`][agents.items.RunItem]이며, 실행 항목은 LLM이 생성한 원문 항목을 감쌉니다.

- [`MessageOutputItem`][agents.items.MessageOutputItem]은 LLM의 메시지를 나타냅니다. 원문 항목은 생성된 메시지입니다.
- [`HandoffCallItem`][agents.items.HandoffCallItem]은 LLM이 핸드오프 도구를 호출했음을 나타냅니다. 원문 항목은 LLM의 도구 호출 항목입니다.
- [`HandoffOutputItem`][agents.items.HandoffOutputItem]은 핸드오프가 발생했음을 나타냅니다. 원문 항목은 핸드오프 도구 호출에 대한 도구 응답입니다. 또한 항목에서 소스/대상 에이전트에 접근할 수 있습니다.
- [`ToolCallItem`][agents.items.ToolCallItem]은 LLM이 도구를 호출했음을 나타냅니다.
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]은 도구가 호출되었음을 나타냅니다. 원문 항목은 도구 응답입니다. 또한 항목에서 도구 출력에 접근할 수 있습니다.
- [`ReasoningItem`][agents.items.ReasoningItem]은 LLM의 추론 항목을 나타냅니다. 원문 항목은 생성된 추론입니다.

## 기타 정보

### 가드레일 결과

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 및 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 속성에는(있는 경우) 가드레일의 결과가 포함됩니다. 가드레일 결과에는 때때로 로그로 남기거나 저장하고 싶은 유용한 정보가 들어 있을 수 있으므로, 이를 사용할 수 있게 제공합니다.

도구 가드레일 결과는 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]로 별도로 제공됩니다. 이러한 가드레일은 도구에 부착할 수 있으며, 해당 도구 호출은 에이전트 워크플로우 동안 가드레일을 실행합니다.

### 원문 응답

[`raw_responses`][agents.result.RunResultBase.raw_responses] 속성에는 LLM이 생성한 [`ModelResponse`][agents.items.ModelResponse]들이 들어 있습니다.

### 원래 입력

[`input`][agents.result.RunResultBase.input] 속성에는 `run` 메서드에 제공한 원래 입력이 들어 있습니다. 대부분의 경우 필요하지 않지만, 필요한 경우를 위해 제공됩니다.

### 인터럽션(중단 처리) 및 실행 재개

실행이 도구 승인 때문에 일시 중지되는 경우, 대기 중인 승인은 [`interruptions`][agents.result.RunResultBase.interruptions]에 노출됩니다. 결과를 `to_state()`로 [`RunState`][agents.run_state.RunState]로 변환하고, 인터럽션을 승인 또는 거부한 뒤, `Runner.run(...)` 또는 `Runner.run_streamed(...)`로 재개하세요.

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

[`RunResult`][agents.result.RunResult]와 [`RunResultStreaming`][agents.result.RunResultStreaming] 모두 `to_state()`를 지원합니다.

### 편의 헬퍼

`RunResultBase`에는 프로덕션 플로우에서 유용한 몇 가지 헬퍼 메서드/속성이 포함되어 있습니다:

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as]는 최종 출력을 특정 타입으로 캐스팅합니다(선택적으로 런타임 타입 검사 포함).
- [`last_response_id`][agents.result.RunResultBase.last_response_id]는 최신 모델 응답 ID를 반환하며, 응답 체이닝에 유용합니다.
- [`release_agents(...)`][agents.result.RunResultBase.release_agents]는 결과를 확인한 뒤 메모리 부담을 줄이고 싶을 때 에이전트에 대한 강한 참조를 해제합니다.