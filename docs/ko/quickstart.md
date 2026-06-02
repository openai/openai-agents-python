---
search:
  exclude: true
---
# 빠른 시작

## 프로젝트 및 가상 환경 생성

이 작업은 한 번만 수행하면 됩니다.

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 가상 환경 활성화

새 터미널 세션을 시작할 때마다 이 작업을 수행하세요.

macOS 또는 Linux:

```bash
source .venv/bin/activate
```

Windows:

```cmd
.venv\Scripts\activate
```

### Agents SDK 설치

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API 키 설정

API 키가 없다면 [이 지침](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)에 따라 OpenAI API 키를 생성하세요.

이 명령은 현재 터미널 세션에 키를 설정합니다.

macOS 또는 Linux:

```bash
export OPENAI_API_KEY=sk-...
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

Windows Command Prompt:

```cmd
set "OPENAI_API_KEY=sk-..."
```

## 첫 에이전트 생성

에이전트는 instructions, 이름, 특정 모델과 같은 선택적 구성으로 정의됩니다.

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 첫 에이전트 실행

[`Runner`][agents.run.Runner]를 사용해 에이전트를 실행하고 [`RunResult`][agents.result.RunResult]를 반환받습니다.

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

두 번째 턴에서는 `result.to_input_list()`를 다시 `Runner.run(...)`에 전달하거나, [세션](sessions/index.md)을 연결하거나, `conversation_id` / `previous_response_id`를 사용해 OpenAI 서버 관리 상태를 재사용할 수 있습니다. [에이전트 실행](running_agents.md) 가이드에서는 이러한 접근 방식을 비교합니다.

다음 경험칙을 사용하세요.

| 원하는 경우... | 시작점... |
| --- | --- |
| 전체 수동 제어와 제공자에 독립적인 기록 | `result.to_input_list()` |
| SDK가 기록을 로드하고 저장하도록 함 | [`session=...`](sessions/index.md) |
| OpenAI가 관리하는 서버 측 이어가기 | `previous_response_id` 또는 `conversation_id` |

절충점과 정확한 동작은 [에이전트 실행](running_agents.md#choose-a-memory-strategy)을 참고하세요.

작업이 주로 프롬프트, 도구, 대화 상태 안에서 이루어진다면 일반 `Agent`와 `Runner`를 사용하세요. 에이전트가 격리된 워크스페이스에서 실제 파일을 검사하거나 수정해야 한다면 [샌드박스 에이전트 빠른 시작](sandbox_agents.md)으로 이동하세요.

## 에이전트에 도구 제공

에이전트에 정보를 조회하거나 작업을 수행할 수 있는 도구를 제공할 수 있습니다.

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

멀티 에이전트 패턴을 선택하기 전에, 최종 답변을 누가 담당할지 결정하세요.

-   **핸드오프**: 해당 턴의 해당 부분에 대해 전문가가 대화를 이어받습니다.
-   **Agents as tools**: 오케스트레이터가 제어를 유지하며 전문가를 도구로 호출합니다.

이 빠른 시작에서는 첫 예제로 가장 짧기 때문에 **핸드오프**를 계속 사용합니다. 매니저 스타일 패턴은 [에이전트 오케스트레이션](multi_agent.md) 및 [도구: agents as tools](tools.md#agents-as-tools)를 참고하세요.

추가 에이전트도 같은 방식으로 정의할 수 있습니다. `handoff_description`은 라우팅 에이전트가 언제 위임할지에 대한 추가 컨텍스트를 제공합니다.

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

에이전트에서는 작업을 해결하는 동안 선택할 수 있는 발신 핸드오프 옵션 목록을 정의할 수 있습니다.

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 에이전트 오케스트레이션 실행

러너는 개별 에이전트 실행, 모든 핸드오프, 모든 도구 호출을 처리합니다.

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

## 참조 예제

저장소에는 동일한 핵심 패턴에 대한 전체 스크립트가 포함되어 있습니다.

-   [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py): 첫 실행
-   [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py): 함수 도구
-   [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py): 멀티 에이전트 라우팅

## 트레이스 보기

에이전트 실행 중 발생한 일을 검토하려면 [OpenAI Dashboard의 Trace viewer](https://platform.openai.com/traces)로 이동해 에이전트 실행 트레이스를 확인하세요.

## 다음 단계

더 복잡한 에이전트형 흐름을 구축하는 방법을 알아보세요.

-   [에이전트](agents.md) 구성 방법 알아보기
-   [에이전트 실행](running_agents.md) 및 [세션](sessions/index.md) 알아보기
-   실제 워크스페이스 안에서 작업이 이루어져야 하는 경우 [샌드박스 에이전트](sandbox_agents.md) 알아보기
-   [도구](tools.md), [가드레일](guardrails.md), [모델](models/index.md) 알아보기