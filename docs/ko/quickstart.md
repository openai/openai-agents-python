---
search:
  exclude: true
---
# 빠른 시작

## 프로젝트 및 가상 환경 생성

이 작업은 한 번만 하면 됩니다

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 가상 환경 활성화

새 터미널 세션을 시작할 때마다 이 작업을 수행하세요

```bash
source .venv/bin/activate
```

### Agents SDK 설치

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API 키 설정

아직 키가 없다면 [이 지침](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)을 따라 OpenAI API 키를 생성하세요

```bash
export OPENAI_API_KEY=sk-...
```

## 첫 에이전트 생성

에이전트는 instructions, 이름, 그리고 특정 모델 같은 선택적 구성으로 정의됩니다

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 첫 에이전트 실행

[`Runner`][agents.run.Runner]를 사용해 에이전트를 실행하고 [`RunResult`][agents.result.RunResult]를 반환받으세요

```python
import asyncio
from agents import Agent, Runner

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)

async def main():
    result = await Runner.run(agent, "When did the Roman Empire fall?")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

두 번째 턴에서는 `result.to_input_list()`를 `Runner.run(...)`에 다시 전달하거나, [session](sessions/index.md)을 연결하거나, `conversation_id` / `previous_response_id`로 OpenAI 서버 관리 상태를 재사용할 수 있습니다. [에이전트 실행](running_agents.md) 가이드에서 이러한 접근 방식을 비교합니다

이 경험칙을 사용하세요:

| 원한다면... | 시작 방법... |
| --- | --- |
| 완전한 수동 제어와 provider-agnostic 기록 | `result.to_input_list()` |
| SDK가 기록을 대신 불러오고 저장하기를 원함 | [`session=...`](sessions/index.md) |
| OpenAI 관리 서버 측 이어서 실행 | `previous_response_id` 또는 `conversation_id` |

트레이드오프와 정확한 동작은 [에이전트 실행](running_agents.md#choose-a-memory-strategy)을 참고하세요

## 에이전트에 도구 제공

에이전트에 정보를 조회하거나 작업을 수행할 수 있는 도구를 제공할 수 있습니다

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool
def history_fun_fact() -> str:
    """Return a short history fact."""
    return "Sharks are older than trees."


agent = Agent(
    name="History Tutor",
    instructions="Answer history questions clearly. Use history_fun_fact when it helps.",
    tools=[history_fun_fact],
)


async def main():
    result = await Runner.run(
        agent,
        "Tell me something surprising about ancient life on Earth.",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

## 에이전트 몇 개 더 추가

멀티 에이전트 패턴을 선택하기 전에, 최종 답변을 누가 담당할지 결정하세요:

-   **핸드오프**: 해당 턴의 그 부분에서는 전문 에이전트가 대화를 이어받습니다
-   **Agents as tools**: 오케스트레이터가 제어를 유지하고 전문 에이전트를 도구로 호출합니다

이 빠른 시작은 가장 짧은 첫 예제이므로 **핸드오프**를 계속 사용합니다. 매니저 스타일 패턴은 [에이전트 오케스트레이션](multi_agent.md) 및 [도구: Agents as tools](tools.md#agents-as-tools)을 참고하세요

추가 에이전트도 같은 방식으로 정의할 수 있습니다. `handoff_description`은 라우팅 에이전트에 언제 위임할지에 대한 추가 컨텍스트를 제공합니다

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You answer history questions clearly and concisely.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You explain math step by step and include worked examples.",
)
```

## 핸드오프 정의

에이전트에서 작업 해결 중 선택할 수 있는 외부 핸드오프 옵션 목록을 정의할 수 있습니다

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 에이전트 오케스트레이션 실행

러너는 개별 에이전트 실행, 핸드오프, 도구 호출을 모두 처리합니다

```python
import asyncio
from agents import Runner


async def main():
    result = await Runner.run(
        triage_agent,
        "Who was the first president of the United States?",
    )
    print(result.final_output)
    print(f"Answered by: {result.last_agent.name}")


if __name__ == "__main__":
    asyncio.run(main())
```

## 참고 코드 예제

리포지토리에는 동일한 핵심 패턴에 대한 전체 스크립트가 포함되어 있습니다:

-   첫 실행용 [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)
-   함수 도구용 [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)
-   멀티 에이전트 라우팅용 [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)

## 트레이스 확인

에이전트 실행 중 무엇이 발생했는지 검토하려면 [OpenAI Dashboard의 Trace viewer](https://platform.openai.com/traces)로 이동해 에이전트 실행의 트레이스를 확인하세요

## 다음 단계

더 복잡한 에이전트 흐름을 구축하는 방법을 알아보세요:

-   [Agents](agents.md) 구성 방법 알아보기
-   [에이전트 실행](running_agents.md) 및 [sessions](sessions/index.md) 알아보기
-   [도구](tools.md), [가드레일](guardrails.md), [모델](models/index.md) 알아보기