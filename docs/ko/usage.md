---
search:
  exclude: true
---
# 사용

Agents SDK는 모든 실행에 대해 토큰 사용량을 자동으로 추적합니다. 실행 컨텍스트에서 이를 확인하여 비용 모니터링, 제한 적용, 분석 기록에 활용할 수 있습니다.

## 추적 항목

- **requests**: 수행된 LLM API 호출 수
- **input_tokens**: 전송된 총 입력 토큰 수
- **output_tokens**: 수신된 총 출력 토큰 수
- **total_tokens**: 입력 + 출력
- **request_usage_entries**: 요청별 사용량 세부 내역 목록
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 실행에서 사용량 접근

`Runner.run(...)` 이후 `result.context_wrapper.usage`를 통해 사용량에 접근할 수 있습니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

사용량은 실행 중 발생한 모든 모델 호출(도구 호출 및 핸드오프 포함)에 걸쳐 집계됩니다.

### 서드파티 어댑터에서 사용량 활성화

사용량 보고는 서드파티 어댑터와 제공자 백엔드에 따라 달라집니다. 어댑터 기반 모델을 사용하고 정확한 `result.context_wrapper.usage` 값이 필요하다면 다음을 확인하세요:

- `AnyLLMModel`에서는 업스트림 제공자가 사용량을 반환하면 자동으로 전파됩니다. 스트리밍 Chat Completions 백엔드의 경우, 사용량 청크가 전송되기 전에 `ModelSettings(include_usage=True)`가 필요할 수 있습니다
- `LitellmModel`에서는 일부 제공자 백엔드가 기본적으로 사용량을 보고하지 않으므로, `ModelSettings(include_usage=True)`가 자주 필요합니다

모델 가이드의 [서드파티 어댑터](models/index.md#third-party-adapters) 섹션에서 어댑터별 참고 사항을 확인하고, 배포 예정인 정확한 제공자 백엔드를 검증하세요.

## 요청별 사용량 추적

SDK는 `request_usage_entries`에서 각 API 요청의 사용량을 자동으로 추적하므로, 상세한 비용 계산과 컨텍스트 윈도 소비량 모니터링에 유용합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 세션에서 사용량 접근

`Session`(예: `SQLiteSession`)을 사용할 때 `Runner.run(...)`의 각 호출은 해당 실행에 대한 사용량을 반환합니다. 세션은 컨텍스트를 위해 대화 이력을 유지하지만, 각 실행의 사용량은 서로 독립적입니다.

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

세션은 실행 간 대화 컨텍스트를 보존하지만, 각 `Runner.run()` 호출에서 반환되는 사용량 지표는 해당 실행만을 나타냅니다. 세션에서는 이전 메시지가 각 실행의 입력으로 다시 주입될 수 있으며, 이는 이후 턴의 입력 토큰 수에 영향을 줍니다.

## 훅에서 사용량 활용

`RunHooks`를 사용하는 경우, 각 훅에 전달되는 `context` 객체에 `usage`가 포함됩니다. 이를 통해 주요 라이프사이클 시점에 사용량을 기록할 수 있습니다.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 레퍼런스

자세한 API 문서는 다음을 참고하세요:

-   [`Usage`][agents.usage.Usage] - 사용량 추적 데이터 구조
-   [`RequestUsage`][agents.usage.RequestUsage] - 요청별 사용량 세부 정보
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 실행 컨텍스트에서 사용량 접근
-   [`RunHooks`][agents.run.RunHooks] - 사용량 추적 라이프사이클에 훅 연결