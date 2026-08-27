---
search:
  exclude: true
---
# 사용량

Agents SDK는 모든 실행의 토큰 사용량을 자동으로 추적합니다. 실행 컨텍스트에서 사용량에 접근하여 비용을 모니터링하거나, 한도를 적용하거나, 분석 데이터를 기록할 수 있습니다.

## 추적 항목 {#what-is-tracked}

- **requests**: 수행된 LLM API 호출 수
- **input_tokens**: 전송된 총 입력 토큰 수
- **output_tokens**: 수신된 총 출력 토큰 수
- **total_tokens**: 입력 + 출력
- **request_usage_entries**: 요청별 사용량 세부 내역 목록
- **details**:
  - `input_tokens_details.cached_tokens`
  - `input_tokens_details.cache_write_tokens`
  - `output_tokens_details.reasoning_tokens`

## 실행에서 사용량 접근 {#accessing-usage-from-a-run}

`Runner.run(...)` 실행 후 `result.context_wrapper.usage`를 통해 사용량에 접근합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

사용량은 도구 호출이나 핸드오프를 생성하는 모델 호출을 포함하여 실행 중 발생한 모든 모델 호출에 걸쳐 집계됩니다.

[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]가 실행이 완료되기 전에 기록을 자동으로 압축하면 해당 `responses.compact` 요청에서 보고된 사용량도 같은 실행의 합계에 추가됩니다. 실행 외부에서 수동으로 수행한 `run_compaction()` 호출에는 이를 포함하는 실행 컨텍스트가 없으므로 이전 실행에서 반환된 사용량 객체를 업데이트하지 않습니다. [OpenAI Responses 압축 세션](sessions/index.md#openai-responses-compaction-sessions)을 참고하세요.

### 서드 파티 어댑터의 사용량 활성화 {#enabling-usage-with-third-party-adapters}

사용량 보고 방식은 서드 파티 어댑터와 제공자 백엔드에 따라 다릅니다. 서드 파티 어댑터를 통해 모델에 접근하면서 정확한 `result.context_wrapper.usage` 값이 필요한 경우 다음 사항을 참고하세요.

- `AnyLLMModel`에서는 업스트림 제공자가 사용량을 반환할 때 자동으로 전달됩니다. Chat Completions 백엔드에서 응답을 스트리밍할 때 사용량 청크가 생성되도록 하려면 `ModelSettings(include_usage=True)`이 필요할 수 있습니다.
- `LitellmModel`에서는 일부 제공자 백엔드가 기본적으로 사용량을 보고하지 않으므로 `ModelSettings(include_usage=True)`가 필요한 경우가 많습니다.

Models 가이드의 [서드 파티 어댑터](models/index.md#third-party-adapters) 섹션에서 어댑터별 참고 사항을 확인하고, 배포하려는 제공자 백엔드에서 사용량이 정확하게 보고되는지 검증하세요.

## 요청별 사용량 추적 {#per-request-usage-tracking}

SDK는 각 API 요청의 사용량을 `request_usage_entries`에서 자동으로 추적합니다. 이는 상세한 비용 계산과 컨텍스트 창 사용량 모니터링에 유용합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 제공자 사용량 페이로드 보존 {#preserving-provider-usage-payloads}

Agents SDK는 제공자 사용량을 여러 모델 제공자에서 일관된 합계를 제공하는 [`Usage`][agents.usage.Usage] 필드로 정규화합니다. 애플리케이션에서 제공자별 사용량 필드를 유지하거나 생략된 필드와 제공자가 보고한 0을 구분해야 하는 경우 [`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage]를 `True`으로 설정합니다.

```python
from agents import Agent, ModelSettings, Runner

agent = Agent(
    name="Assistant",
    model_settings=ModelSettings(preserve_raw_usage=True),
)
result = await Runner.run(agent, "What's the weather in Tokyo?")

for response in result.raw_responses:
    print(response.raw_usage)
```

Agents SDK는 각 [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] 값을 해당 모델 호출에 대한 제공자 페이로드의 분리된 JSON 호환 스냅샷으로 저장합니다. Agents SDK는 실행 전체에서 `raw_usage`을 집계하지 않습니다. 보존이 비활성화되어 있거나, 제공자가 사용량 페이로드를 반환하지 않거나, 업스트림 어댑터가 원래의 필드 존재 여부 정보를 이미 삭제한 경우 이 값은 `None`으로 유지됩니다.

`preserve_raw_usage`은 모델 어댑터에 도달한 사용량 페이로드만 보존하며, 이 설정이 제공자에게 사용량을 요청하지는 않습니다. 스트리밍 Chat Completions 제공자가 명시적인 사용량 요청을 요구하는 경우 `ModelSettings(include_usage=True)`도 설정합니다.

`LitellmModel`는 현재 스트리밍 및 비스트리밍 실행 모두에서 `ModelResponse.raw_usage`을 채우지 않으므로 `preserve_raw_usage=True`는 해당 어댑터에서 효과가 없습니다. `LitellmModel`을 사용할 때는 계속해서 정규화된 [`Usage`][agents.usage.Usage] 필드를 사용하거나, 제공자별 필드 존재 여부가 필요한 경우 raw 사용량 보존을 지원하는 어댑터를 선택하세요.

## 세션에서 사용량 접근 {#accessing-usage-with-sessions}

`Session`(예: `SQLiteSession`)을 사용하는 경우 `Runner.run(...)`를 호출할 때마다 해당 실행의 사용량이 반환됩니다. 세션은 컨텍스트를 위해 대화 기록을 유지하지만 각 실행의 사용량은 독립적입니다.

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

세션은 실행 간 대화 컨텍스트를 보존하지만, 각 `Runner.run()` 호출에서 반환되는 사용량 지표는 해당 실행만 나타냅니다. 세션에서는 이전 메시지가 각 실행의 입력으로 다시 제공될 수 있으며, 이는 이후 턴의 입력 토큰 수에 영향을 줍니다.

## RunState 체크포인트의 사용량 {#usage-in-runstate-checkpoints}

[`RunResult.to_state()`][agents.result.RunResult.to_state]는 그 시점까지 누적된 사용량의 독립적인 스냅샷을 캡처합니다. 해당 체크포인트에서 재개된 실행은 캡처된 합계로 시작하며 자체 모델 호출의 사용량을 추가합니다. 재개된 실행은 이러한 새 합계를 원래 `RunResult` 또는 해당 결과에서 생성된 다른 체크포인트에 추가하지 않습니다.

```python
first = await Runner.run(agent, "First request")
checkpoint_a = first.to_state()
checkpoint_b = first.to_state()

resumed_a = await Runner.run(agent, checkpoint_a)
resumed_b = await Runner.run(agent, checkpoint_b)

assert resumed_a.context_wrapper.usage is not first.context_wrapper.usage
assert resumed_b.context_wrapper.usage is not resumed_a.context_wrapper.usage
```

이러한 격리는 [`Usage`][agents.usage.Usage] 내부의 `request_usage_entries` 목록에도 적용됩니다. 재개된 중첩 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행은 독립적인 최상위 사용량 집계의 예외입니다. 재개 후의 모델 사용량은 중첩 실행의 이전 모델 호출과 마찬가지로 활성 외부 실행의 사용량에 의도적으로 집계됩니다.

## 훅에서 사용량 활용 {#using-usage-in-hooks}

`RunHooks`을 사용하는 경우 각 훅에 전달되는 `context` 객체에는 `usage`이 포함됩니다. 이를 통해 주요 수명 주기 시점에 사용량을 기록할 수 있습니다.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 레퍼런스 {#api-reference}

자세한 API 문서는 다음을 참고하세요.

-   [`Usage`][agents.usage.Usage] - 사용량 추적 데이터 구조
-   [`RequestUsage`][agents.usage.RequestUsage] - 요청별 사용량 세부 정보
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 실행 컨텍스트에서 사용량 접근
-   [`RunHooks`][agents.run.RunHooks] - 사용량 추적 수명 주기에 연결