---
search:
  exclude: true
---
# 사용량

Agents SDK는 실행마다 토큰 사용량을 자동으로 추적합니다. 실행 컨텍스트에서 접근하여 비용 모니터링, 한도 적용, 분석 기록에 사용할 수 있습니다.

## 추적 항목

- **requests**: 수행된 LLM API 호출 수
- **input_tokens**: 전송된 입력 토큰 총합
- **output_tokens**: 수신된 출력 토큰 총합
- **total_tokens**: input + output
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 실행에서 사용량 접근

`Runner.run(...)` 이후, `result.context_wrapper.usage`를 통해 사용량에 접근합니다.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

사용량은 실행 중 이루어진 모든 모델 호출 전반에 걸쳐 집계됩니다(도구 호출과 핸드오프 포함).

### LiteLLM 모델에서 사용량 활성화

LiteLLM 제공자는 기본적으로 사용량 지표를 보고하지 않습니다. [`LitellmModel`](models/litellm.md)을 사용할 때, 에이전트에 `ModelSettings(include_usage=True)`를 전달하면 LiteLLM 응답이 `result.context_wrapper.usage`를 채웁니다.

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

## 세션에서 사용량 접근

`Session`(예: `SQLiteSession`)을 사용할 때는 `Runner.run(...)` 호출마다 해당 실행에 대한 사용량이 반환됩니다. 세션은 컨텍스트를 위한 대화 기록을 유지하지만, 각 실행의 사용량은 독립적입니다.

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

세션은 실행 간 대화 컨텍스트를 보존하지만, 각 `Runner.run()` 호출이 반환하는 사용량 지표는 해당 실행만을 나타냅니다. 세션에서는 이전 메시지가 각 실행의 입력으로 다시 제공될 수 있으며, 이는 이후 턴의 입력 토큰 수에 영향을 줍니다.

## 훅에서 사용량 활용

`RunHooks`를 사용하는 경우, 각 훅에 전달되는 `context` 객체에 `usage`가 포함됩니다. 이를 통해 수명 주기의 주요 시점에서 사용량을 로깅할 수 있습니다.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 참고

자세한 API 문서는 다음을 참조하세요:

-   [`Usage`][agents.usage.Usage] - 사용량 추적 데이터 구조
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 실행 컨텍스트에서 사용량 접근
-   [`RunHooks`][agents.run.RunHooks] - 사용량 트래킹 수명 주기에 훅 연결