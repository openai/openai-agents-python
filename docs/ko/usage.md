---
search:
  exclude: true
---
# 사용량

Agents SDK 는 각 실행(run)의 토큰 사용량을 자동으로 추적합니다. 실행 컨텍스트에서 이를 확인하여 비용 모니터링, 제한 적용, 분석 기록에 사용할 수 있습니다.

## 추적 항목

- **requests**: 수행된 LLM API 호출 수
- **input_tokens**: 전송된 입력 토큰 총합
- **output_tokens**: 수신된 출력 토큰 총합
- **total_tokens**: 입력 + 출력
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 실행에서 사용량 접근

`Runner.run(...)` 이후, `result.context_wrapper.usage` 를 통해 사용량에 접근합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

사용량은 실행 중 발생한 모든 모델 호출(도구 호출과 핸드오프 포함)을 합산하여 집계됩니다.

## 세션에서 사용량 접근

`Session`(예: `SQLiteSession`)을 사용하는 경우, 동일한 실행 내 여러 턴에 걸쳐 사용량이 계속 누적됩니다. 각 `Runner.run(...)` 호출은 해당 시점의 실행 누적 사용량을 반환합니다.

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # includes both turns
```

## 훅에서 사용량 활용

`RunHooks` 를 사용하는 경우, 각 훅에 전달되는 `context` 객체에 `usage` 가 포함됩니다. 이를 통해 주요 라이프사이클 시점에 사용량을 기록할 수 있습니다.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```