---
search:
  exclude: true
---
# 빠른 시작

## 프로젝트 및 가상 환경 생성 {#create-a-project-and-virtual-environment}

이 작업은 한 번만 수행하면 됩니다.

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 가상 환경 활성화 {#activate-the-virtual-environment}

새 터미널 세션을 시작할 때마다 수행합니다.

macOS 또는 Linux:

```bash
source .venv/bin/activate
```

Windows:

```cmd
.venv\Scripts\activate
```

### Agents SDK 설치 {#install-the-agents-sdk}

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API 키 설정 {#set-an-openai-api-key}

키가 없다면 [다음 안내](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)에 따라 OpenAI API 키를 생성하세요.

다음 명령은 현재 터미널 세션에 키를 설정합니다.

macOS 또는 Linux:

```bash
export OPENAI_API_KEY=sk-...
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

Windows 명령 프롬프트:

```cmd
set "OPENAI_API_KEY=sk-..."
```

## 첫 번째 에이전트 생성 {#create-your-first-agent}

에이전트는 instructions, 이름, 특정 모델과 같은 선택적 구성으로 정의됩니다.

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 첫 번째 에이전트 실행 {#run-your-first-agent}

[`Runner`][agents.run.Runner]을 사용하여 에이전트를 실행하고 [`RunResult`][agents.result.RunResult]을 반환받습니다.

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

두 번째 턴에서는 `result.to_input_list()`을 `Runner.run(...)`에 다시 전달하거나, [세션](sessions/index.md)을 연결하거나, `conversation_id` / `previous_response_id`을 사용해 OpenAI 서버 관리 상태를 재사용할 수 있습니다. [에이전트 실행](running_agents.md) 가이드에서는 이러한 접근 방식을 비교합니다.

다음 기준을 활용하세요.

| 원하는 사항 | 시작 방법 |
| --- | --- |
| 완전한 수동 제어 및 제공자 독립적인 기록 | `result.to_input_list()` |
| SDK를 통한 기록 불러오기 및 저장 | [`session=...`](sessions/index.md) |
| OpenAI가 관리하는 서버 측 연속 실행 | `previous_response_id` 또는 `conversation_id` |

장단점과 정확한 동작은 [에이전트 실행](running_agents.md#choose-a-memory-strategy)을 참조하세요.

작업이 주로 프롬프트, 도구, 대화 상태로 구성되는 경우 일반 `Agent`과 `Runner`을 함께 사용하세요. 에이전트가 격리된 작업 공간에서 실제 파일을 검사하거나 수정해야 한다면 [샌드박스 에이전트 빠른 시작](sandbox_agents.md)으로 이동하세요.

## 에이전트에 도구 제공 {#give-your-agent-tools}

에이전트에 정보를 조회하거나 작업을 수행할 수 있는 도구를 제공할 수 있습니다.

```python
import asyncio
from agents import Agent, Runner
from agents.decorators import tool


@tool
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

## 에이전트 추가 {#add-a-few-more-agents}

다중 에이전트 패턴을 선택하기 전에 최종 답변을 누가 담당할지 결정하세요.

-   **핸드오프**: 전문 에이전트가 해당 턴의 일부에 대한 대화를 넘겨받습니다.
-   **Agents as tools**: 오케스트레이터가 제어권을 유지하면서 전문 에이전트를 도구로 호출합니다.

이 빠른 시작에서는 첫 번째 예제로 가장 간단한 **핸드오프**를 계속 사용합니다. 관리자 스타일 패턴은 [에이전트 오케스트레이션](multi_agent.md) 및 [도구: Agents as tools](tools.md#agents-as-tools)을 참조하세요.

추가 에이전트도 같은 방식으로 정의할 수 있습니다. `handoff_description`은 라우팅 에이전트에 언제 위임해야 하는지에 대한 추가 컨텍스트를 제공합니다.

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

## 핸드오프 정의 {#define-your-handoffs}

에이전트에는 작업을 해결하는 동안 선택할 수 있는 발신 핸드오프 옵션 목록을 정의할 수 있습니다.

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 에이전트 오케스트레이션 실행 {#run-the-agent-orchestration}

러너는 개별 에이전트 실행, 모든 핸드오프 및 모든 도구 호출을 처리합니다.

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

## 참조 코드 예제 {#reference-examples}

리포지토리에는 동일한 핵심 패턴을 보여 주는 전체 스크립트가 포함되어 있습니다.

-   첫 번째 실행을 위한 [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)
-   함수 도구를 위한 [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)
-   다중 에이전트 라우팅을 위한 [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)

## 트레이스 확인 {#view-your-traces}

에이전트 실행 중 발생한 작업을 검토하려면 [OpenAI 대시보드의 트레이스 뷰어](https://platform.openai.com/traces)로 이동하여 에이전트 실행의 트레이스를 확인하세요.

## 다음 단계 {#next-steps}

더 복잡한 에이전트 워크플로를 구축하는 방법을 알아보세요.

-   [에이전트](agents.md) 구성 방법 알아보기
-   [에이전트 실행](running_agents.md) 및 [세션](sessions/index.md) 알아보기
-   실제 작업 공간에서 작업해야 하는 경우 [샌드박스 에이전트](sandbox_agents.md) 알아보기
-   [도구](tools.md), [가드레일](guardrails.md), [모델](models/index.md) 알아보기