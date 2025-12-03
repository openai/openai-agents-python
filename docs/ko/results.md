---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 다음 중 하나를 받습니다:

- [`RunResult`][agents.result.RunResult] 를 `run` 또는 `run_sync` 를 호출한 경우
- [`RunResultStreaming`][agents.result.RunResultStreaming] 를 `run_streamed` 를 호출한 경우

이 둘은 모두 [`RunResultBase`][agents.result.RunResultBase] 를 상속하며, 대부분의 유용한 정보는 여기에 포함되어 있습니다.

## 최종 출력

[`final_output`][agents.result.RunResultBase.final_output] 속성에는 마지막으로 실행된 에이전트의 최종 출력이 포함됩니다. 이는 다음 중 하나입니다:

- 마지막 에이전트에 `output_type` 이 정의되지 않은 경우 `str`
- 에이전트에 출력 타입이 정의된 경우 `last_agent.output_type` 타입의 객체

!!! note

    `final_output` 의 타입은 `Any` 입니다. 핸드오프 때문에 이를 정적으로 타이핑할 수 없습니다. 핸드오프가 발생하면 어떤 에이전트든 마지막 에이전트가 될 수 있으므로, 가능한 출력 타입 집합을 정적으로 알 수 없습니다.

## 다음 턴 입력

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 를 사용하여 결과를 입력 리스트로 변환할 수 있습니다. 이는 여러분이 제공한 원래 입력에 에이전트 실행 중 생성된 항목을 이어붙입니다. 이를 통해 한 번의 에이전트 실행 출력을 다른 실행에 전달하거나, 루프에서 실행하며 매번 새로운 사용자 입력을 추가하기가 편리합니다.

## 마지막 에이전트

[`last_agent`][agents.result.RunResultBase.last_agent] 속성에는 마지막으로 실행된 에이전트가 포함됩니다. 애플리케이션에 따라, 이는 다음번에 사용자 입력이 들어올 때 유용한 경우가 많습니다. 예를 들어, 1차 분류 에이전트가 언어별 에이전트로 핸드오프하는 구조라면, 마지막 에이전트를 저장해 두었다가 사용자가 다음에 메시지를 보낼 때 재사용할 수 있습니다.

## 새 항목

[`new_items`][agents.result.RunResultBase.new_items] 속성에는 실행 중 생성된 새 항목이 들어 있습니다. 항목은 [`RunItem`][agents.items.RunItem] 들입니다. 실행 항목은 LLM 이 생성한 원문 항목을 래핑합니다.

- [`MessageOutputItem`][agents.items.MessageOutputItem] 은 LLM 의 메시지를 나타냅니다. 원문 항목은 생성된 메시지입니다
- [`HandoffCallItem`][agents.items.HandoffCallItem] 은 LLM 이 핸드오프 도구를 호출했음을 나타냅니다. 원문 항목은 LLM 의 도구 호출 항목입니다
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] 은 핸드오프가 발생했음을 나타냅니다. 원문 항목은 핸드오프 도구 호출에 대한 도구 응답입니다. 항목에서 소스/타깃 에이전트에도 접근할 수 있습니다
- [`ToolCallItem`][agents.items.ToolCallItem] 은 LLM 이 도구를 호출했음을 나타냅니다
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 은 도구가 호출되었음을 나타냅니다. 원문 항목은 도구 응답입니다. 항목에서 도구 출력에도 접근할 수 있습니다
- [`ReasoningItem`][agents.items.ReasoningItem] 은 LLM 의 추론 항목을 나타냅니다. 원문 항목은 생성된 추론입니다

## 기타 정보

### 가드레일 결과

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 와 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 속성에는 가드레일의 결과가(있다면) 포함됩니다. 가드레일 결과에는 기록하거나 저장하고 싶은 유용한 정보가 포함될 수 있어, 이를 제공해 드립니다.

### 원문 응답

[`raw_responses`][agents.result.RunResultBase.raw_responses] 속성에는 LLM 이 생성한 [`ModelResponse`][agents.items.ModelResponse] 들이 포함됩니다.

### 원본 입력

[`input`][agents.result.RunResultBase.input] 속성에는 `run` 메서드에 제공한 원본 입력이 들어 있습니다. 대부분의 경우 필요하지 않지만, 필요할 때를 대비해 제공됩니다.