---
search:
  exclude: true
---
# 결과

`Runner.run` 메서드를 호출하면 다음 중 하나를 받습니다:

-   [`RunResult`][agents.result.RunResult] (`run` 또는 `run_sync` 호출 시)
-   [`RunResultStreaming`][agents.result.RunResultStreaming] (`run_streamed` 호출 시)

두 결과 모두 [`RunResultBase`][agents.result.RunResultBase]를 상속하며, 대부분의 유용한 정보가 여기에 포함됩니다.

## 최종 출력

[`final_output`][agents.result.RunResultBase.final_output] 프로퍼티에는 마지막으로 실행된 에이전트의 최종 출력이 포함됩니다. 다음 중 하나입니다:

-   마지막 에이전트에 `output_type`이 정의되지 않았다면 `str`
-   에이전트에 출력 타입이 정의되어 있다면 `last_agent.output_type` 타입의 객체

!!! note

    `final_output`의 타입은 `Any`입니다. 핸드오프 때문에 정적으로 타입을 지정할 수 없습니다. 핸드오프가 발생하면 어떤 에이전트든 마지막 에이전트가 될 수 있으므로, 가능한 출력 타입 집합을 정적으로 알 수 없습니다.

## 다음 턴 입력

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list]를 사용하면 결과를 입력 리스트로 변환하여, 제공한 원래 입력과 에이전트 실행 중 생성된 항목들을 연결할 수 있습니다. 이를 통해 한 번의 에이전트 실행 결과를 다른 실행으로 전달하거나, 루프에서 실행하며 매번 새로운 사용자 입력을 덧붙이는 작업이 편리해집니다.

## 마지막 에이전트

[`last_agent`][agents.result.RunResultBase.last_agent] 프로퍼티에는 마지막으로 실행된 에이전트가 포함됩니다. 애플리케이션에 따라, 다음에 사용자가 무언가를 입력할 때 유용한 경우가 많습니다. 예를 들어, 1차 분류 에이전트가 언어별 에이전트로 핸드오프하는 경우, 마지막 에이전트를 저장해 두었다가 다음에 사용자가 메시지를 보낼 때 재사용할 수 있습니다.

## 새 항목

[`new_items`][agents.result.RunResultBase.new_items] 프로퍼티에는 실행 중에 생성된 새 항목이 포함됩니다. 항목은 [`RunItem`][agents.items.RunItem]입니다. 실행 항목은 LLM이 생성한 원문 항목을 래핑합니다.

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM의 메시지를 나타냅니다. 원문 항목은 생성된 메시지입니다
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM이 핸드오프 도구를 호출했음을 나타냅니다. 원문 항목은 LLM의 도구 호출 항목입니다
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: 핸드오프가 발생했음을 나타냅니다. 원문 항목은 핸드오프 도구 호출에 대한 도구 응답입니다. 항목에서 소스/타깃 에이전트에도 접근할 수 있습니다
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM이 도구를 호출했음을 나타냅니다
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: 도구가 호출되었음을 나타냅니다. 원문 항목은 도구 응답입니다. 항목에서 도구 출력에도 접근할 수 있습니다
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM의 추론 항목을 나타냅니다. 원문 항목은 생성된 추론입니다

## 기타 정보

### 가드레일 결과

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results]와 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 프로퍼티에는 (있는 경우) 가드레일의 결과가 포함됩니다. 가드레일 결과에는 로깅하거나 저장하고 싶은 유용한 정보가 담기는 경우가 있어, 이를 제공해 드립니다.

도구 가드레일 결과는 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 및 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]로 별도 제공됩니다. 이러한 가드레일은 도구에 연결될 수 있으며, 해당 도구 호출은 에이전트 워크플로 동안 가드레일을 실행합니다.

### 원문 응답

[`raw_responses`][agents.result.RunResultBase.raw_responses] 프로퍼티에는 LLM이 생성한 [`ModelResponse`][agents.items.ModelResponse]가 포함됩니다.

### 원본 입력

[`input`][agents.result.RunResultBase.input] 프로퍼티에는 `run` 메서드에 제공한 원본 입력이 포함됩니다. 대부분의 경우 필요하지 않지만, 필요할 때를 대비해 제공됩니다.