---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 매우 적은 추상화만으로 에이전트형 AI 앱을 가볍고 사용하기 쉬운 패키지로 구축할 수 있게 해줍니다. 이는 이전의 에이전트 실험용 프레임워크인 [Swarm](https://github.com/openai/swarm/tree/main)을 프로덕션 준비 수준으로 확장한 것입니다. Agents SDK는 매우 작은 기본 구성 요소 집합을 제공합니다.

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **Agents as tools / 핸드오프**: 에이전트가 특정 작업을 위해 다른 에이전트에 위임할 수 있게 해주는 기능
-   **가드레일**: 에이전트 입력과 출력을 검증할 수 있게 해주는 기능

이러한 기본 구성 요소는 Python과 결합될 때 도구와 에이전트 간의 복잡한 관계를 표현할 수 있을 만큼 강력하며, 가파른 학습 곡선 없이도 실제 애플리케이션을 구축할 수 있게 해줍니다. 또한 SDK에는 에이전트형 흐름을 시각화하고 디버그할 수 있을 뿐만 아니라 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝할 수 있도록 해주는 내장 **트레이싱**도 포함되어 있습니다.

## Agents SDK 사용 이유

SDK에는 두 가지 핵심 설계 원칙이 있습니다.

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하면서도, 빠르게 익힐 수 있을 만큼 기본 구성 요소 수는 적게 유지합니다
2. 기본 상태로도 훌륭하게 동작하지만, 정확히 어떤 일이 일어날지 세밀하게 사용자 지정할 수 있습니다

다음은 SDK의 주요 기능입니다.

-   **에이전트 루프**: 도구 호출을 처리하고, 결과를 LLM에 다시 전달하며, 작업이 완료될 때까지 계속하는 내장 에이전트 루프
-   **파이썬 우선**: 새로운 추상화를 배울 필요 없이, 내장 언어 기능을 사용해 에이전트를 오케스트레이션하고 연결합니다
-   **Agents as tools / 핸드오프**: 여러 에이전트에 걸쳐 작업을 조율하고 위임하기 위한 강력한 메커니즘
-   **샌드박스 에이전트**: 매니페스트로 정의된 파일, 샌드박스 클라이언트 선택, 재개 가능한 샌드박스 세션을 갖춘 실제 격리 작업공간 안에서 전문 에이전트를 실행합니다
-   **가드레일**: 에이전트 실행과 병렬로 입력 검증 및 안전성 검사를 수행하고, 검사를 통과하지 못하면 즉시 실패 처리합니다
-   **함수 도구**: 자동 스키마 생성과 Pydantic 기반 검증을 통해 모든 Python 함수를 도구로 변환합니다
-   **MCP 서버 도구 호출**: 함수 도구와 동일한 방식으로 작동하는 내장 MCP 서버 도구 통합
-   **세션**: 에이전트 루프 내에서 작업 컨텍스트를 유지하기 위한 지속형 메모리 계층
-   **휴먼인더루프 (HITL)**: 에이전트 실행 전반에 걸쳐 사람이 개입할 수 있도록 하는 내장 메커니즘
-   **트레이싱**: 워크플로를 시각화, 디버그, 모니터링하기 위한 내장 트레이싱으로, OpenAI의 평가, 파인튜닝, 증류 도구 모음을 지원합니다
-   **실시간 에이전트**: `gpt-realtime-1.5`와 자동 인터럽션(중단 처리) 감지, 컨텍스트 관리, 가드레일 등을 사용해 강력한 음성 에이전트를 구축합니다

## Agents SDK 또는 Responses API

SDK는 OpenAI 모델에 대해 기본적으로 Responses API를 사용하지만, 모델 호출 위에 더 높은 수준의 런타임을 추가로 제공합니다.

다음과 같은 경우에는 Responses API를 직접 사용하세요.

-   루프, 도구 디스패치, 상태 처리를 직접 관리하고 싶은 경우
-   워크플로가 짧게 유지되며 주로 모델의 응답을 반환하는 것이 목적일 경우

다음과 같은 경우에는 Agents SDK를 사용하세요.

-   런타임이 턴, 도구 실행, 가드레일, 핸드오프 또는 세션을 관리하길 원하는 경우
-   에이전트가 아티팩트를 생성하거나 여러 조정된 단계에 걸쳐 작업해야 하는 경우
-   [샌드박스 에이전트](sandbox_agents.md)를 통해 실제 작업공간이나 재개 가능한 실행이 필요한 경우

둘 중 하나를 전역적으로 선택할 필요는 없습니다. 많은 애플리케이션이 관리형 워크플로에는 SDK를 사용하고, 더 낮은 수준의 경로에는 Responses API를 직접 호출합니다.

## 설치

```bash
pip install openai-agents
```

## Hello World 예제

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_이를 실행하려면 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```

## 시작 지점

-   [Quickstart](quickstart.md)로 첫 번째 텍스트 기반 에이전트를 구축하세요
-   그런 다음 [에이전트 실행](running_agents.md#choose-a-memory-strategy)에서 턴 간 상태를 어떻게 유지할지 결정하세요
-   작업이 실제 파일, 저장소 또는 에이전트별로 격리된 작업공간 상태에 의존한다면 [샌드박스 에이전트 빠른 시작](sandbox_agents.md)을 읽어보세요
-   핸드오프와 관리자 스타일 오케스트레이션 중 무엇을 선택할지 결정하고 있다면 [에이전트 오케스트레이션](multi_agent.md)을 읽어보세요

## 경로 선택

원하는 작업은 알고 있지만 어떤 페이지가 이를 설명하는지 모를 때 이 표를 사용하세요.

| 목표 | 시작 지점 |
| --- | --- |
| 첫 번째 텍스트 에이전트를 만들고 하나의 전체 실행을 확인하기 | [Quickstart](quickstart.md) |
| 함수 도구, 호스티드 툴 또는 Agents as tools 추가하기 | [도구](tools.md) |
| 실제 격리 작업공간 안에서 코딩, 리뷰 또는 문서 에이전트 실행하기 | [샌드박스 에이전트 빠른 시작](sandbox_agents.md) 및 [샌드박스 클라이언트](sandbox/clients.md) |
| 핸드오프와 관리자 스타일 오케스트레이션 중 선택하기 | [에이전트 오케스트레이션](multi_agent.md) |
| 턴 간 메모리 유지하기 | [에이전트 실행](running_agents.md#choose-a-memory-strategy) 및 [세션](sessions/index.md) |
| OpenAI 모델, websocket 전송 또는 OpenAI가 아닌 제공자 사용하기 | [모델](models/index.md) |
| 출력, 실행 항목, 인터럽션(중단 처리), 재개 상태 검토하기 | [결과](results.md) |
| `gpt-realtime-1.5`로 저지연 음성 에이전트 구축하기 | [실시간 에이전트 빠른 시작](realtime/quickstart.md) 및 [실시간 전송](realtime/transport.md) |
| speech-to-text / 에이전트 / text-to-speech 파이프라인 구축하기 | [음성 파이프라인 빠른 시작](voice/quickstart.md) |