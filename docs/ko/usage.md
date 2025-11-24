---
search:
  exclude: true
---
# 사용량

Agents SDK는 각 실행에 대한 토큰 사용량을 자동으로 추적합니다. 실행 컨텍스트에서 접근하여 비용 모니터링, 제한 적용, 분석 기록에 사용할 수 있습니다.

## 추적 항목

- **requests**: 수행된 LLM API 호출 수
- **input_tokens**: 전송된 입력 토큰 총합
- **output_tokens**: 수신된 출력 토큰 총합
- **total_tokens**: 입력 + 출력
- **request_usage_entries**: 요청별 사용량 분해 목록
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 실행에서 사용량 접근

`Runner.run(...)` 이후, `result.context_wrapper.usage`로 사용량에 접근합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

사용량은 실행 중의 모든 모델 호출(도구 호출 및 handoffs 포함)에 걸쳐 집계됩니다.

### LiteLLM 모델에서 사용량 활성화

LiteLLM 공급자는 기본적으로 사용량 메트릭을 보고하지 않습니다. [`LitellmModel`](models/litellm.md)을 사용할 때, 에이전트에 `ModelSettings(include_usage=True)`를 전달하면 LiteLLM 응답이 `result.context_wrapper.usage`에 채워집니다.

```python
from agents import Agent, ModelSettings, Runner
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)

result = await Runner.run(agent, "What's the weather in Tokyo?")
print(result.context_wrapper.usage.total_tokens)
```

## 요청별 사용량 추적

SDK는 `request_usage_entries`에서 각 API 요청의 사용량을 자동으로 추적하므로, 상세한 비용 계산 및 컨텍스트 윈도우 소모 모니터링에 유용합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 세션에서 사용량 접근

`Session`(예: `SQLiteSession`)을 사용할 때, `Runner.run(...)`의 각 호출은 해당 실행에 대한 사용량을 반환합니다. 세션은 컨텍스트를 위한 대화 기록을 유지하지만 각 실행의 사용량은 독립적입니다.

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

세션은 실행 간 대화 컨텍스트를 보존하지만, 각 `Runner.run()` 호출이 반환하는 사용량 메트릭은 해당 실행만을 나타냅니다. 세션에서는 이전 메시지가 각 실행의 입력으로 다시 제공될 수 있으며, 이는 이후 턴의 입력 토큰 수에 영향을 줍니다.

## 훅에서 사용량 활용

`RunHooks`를 사용하는 경우, 각 훅에 전달되는 `context` 객체에 `usage`가 포함됩니다. 이를 통해 수명 주기의 주요 시점에 사용량을 기록할 수 있습니다.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## 훅에서 대화 기록 수정하기

`RunContextWrapper`에는 `message_history`도 포함되어 있어 훅에서 현재 대화를 바로 읽거나 수정할 수 있습니다.

- `get_messages()`는 원본 입력, 모델 출력, 보류 중인 삽입 항목을 모두 포함한 전체 기록을 `ResponseInputItem` 리스트로 반환합니다.
- `add_message(agent=..., message=...)`는 사용자 정의 메시지(문자열, 딕셔너리 또는 `ResponseInputItem` 리스트)를 큐에 추가합니다. 추가된 메시지는 즉시 LLM 입력에 이어 붙고 실행 결과/스트림 이벤트에서는 `InjectedInputItem`으로 노출됩니다.
- `override_next_turn(messages)`는 다음 LLM 호출의 입력 전체를 교체할 때 사용합니다. 가드레일이나 외부 검토 결과에 따라 히스토리를 다시 작성해야 할 때 유용합니다.

```python
class BroadcastHooks(RunHooks):
  def __init__(self, reviewer_name: str):
    self.reviewer_name = reviewer_name

  async def on_llm_start(
    self,
    context: RunContextWrapper,
    agent: Agent,
    _instructions: str | None,
    _input_items: list[TResponseInputItem],
  ) -> None:
    context.message_history.add_message(
      agent=agent,
      message={
        "role": "user",
        "content": f"{self.reviewer_name}: 답변 전에 부록 데이터를 인용하세요.",
      },
    )
```

> **참고:** `conversation_id` 또는 `previous_response_id`와 함께 실행하는 경우 서버 측 대화 스레드가 입력을 관리하므로 해당 런에서는 `message_history.override_next_turn()`을 사용할 수 없습니다.

## API 레퍼런스

자세한 API 문서는 다음을 참조하세요:

-   [`Usage`][agents.usage.Usage] - 사용량 추적 데이터 구조
-   [`RequestUsage`][agents.usage.RequestUsage] - 요청별 사용량 세부 정보
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 실행 컨텍스트에서 사용량 접근
-   [`MessageHistory`][agents.run_context.MessageHistory] - 훅에서 대화 기록을 조회/편집
-   [`RunHooks`][agents.run.RunHooks] - 사용량 추적 수명 주기에 훅 연결