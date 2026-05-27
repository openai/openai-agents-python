---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)를 사용하면 매우 적은 추상화만으로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 구축할 수 있습니다. 이는 이전 에이전트 실험 프로젝트인 [Swarm](https://github.com/openai/swarm/tree/main)을 프로덕션에 바로 사용할 수 있도록 업그레이드한 것입니다. Agents SDK는 매우 작은 기본 구성 요소 집합을 갖습니다.

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **Agents as tools / 핸드오프**: 에이전트가 특정 작업을 다른 에이전트에 위임할 수 있게 하는 기능
-   **가드레일**: 에이전트 입력과 출력을 검증할 수 있게 하는 기능

Python과 결합하면 이러한 기본 구성 요소만으로도 도구와 에이전트 간의 복잡한 관계를 표현하기에 충분히 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있습니다. 또한 SDK에는 에이전트형 흐름을 시각화하고 디버깅하며, 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝할 수 있는 내장 **트레이싱** 기능이 포함되어 있습니다.

## Agents SDK를 사용하는 이유

SDK에는 두 가지 핵심 설계 원칙이 있습니다.

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 배울 수 있을 만큼 기본 구성 요소는 적게 유지합니다.
2. 기본 설정만으로도 잘 작동하지만, 어떤 일이 일어나는지는 정확하게 사용자 지정할 수 있습니다.

SDK의 주요 기능은 다음과 같습니다.

-   **에이전트 루프**: 도구 호출을 처리하고, 결과를 LLM에 다시 보내며, 작업이 완료될 때까지 계속 실행하는 내장 에이전트 루프
-   **파이썬 우선**: 새로운 추상화를 배울 필요 없이, 내장 언어 기능을 사용해 에이전트를 오케스트레이션하고 체인으로 연결
-   **Agents as tools / 핸드오프**: 여러 에이전트 간 작업을 조율하고 위임하기 위한 강력한 메커니즘
-   **샌드박스 에이전트**: 매니페스트로 정의된 파일, 샌드박스 클라이언트 선택, 재개 가능한 샌드박스 세션을 통해 실제 격리된 워크스페이스 안에서 전문가 실행
-   **가드레일**: 에이전트 실행과 병렬로 입력 검증 및 안전성 검사를 실행하고, 검사를 통과하지 못하면 빠르게 실패 처리
-   **함수 도구**: 자동 스키마 생성 및 Pydantic 기반 검증을 통해 모든 Python 함수를 도구로 변환
-   **MCP 서버 도구 호출**: 함수 도구와 동일한 방식으로 작동하는 내장 MCP 서버 도구 통합
-   **세션**: 에이전트 루프 내에서 작업 컨텍스트를 유지하기 위한 영속 메모리 계층
-   **휴먼인더루프 (HITL)**: 에이전트 실행 전반에 사람을 참여시키기 위한 내장 메커니즘
-   **트레이싱**: OpenAI의 평가, 파인튜닝, 증류 도구 모음 지원과 함께 워크플로를 시각화, 디버깅, 모니터링하기 위한 내장 트레이싱
-   **실시간 에이전트**: `gpt-realtime-2`, 자동 인터럽션(중단 처리) 감지, 컨텍스트 관리, 가드레일 등을 활용해 강력한 음성 에이전트 구축

## Agents SDK 또는 Responses API

SDK는 OpenAI 모델에 기본적으로 Responses API를 사용하지만, 모델 호출 주변에 더 높은 수준의 런타임을 추가합니다.

다음과 같은 경우 Responses API를 직접 사용하세요.

-   루프, 도구 디스패치, 상태 처리를 직접 관리하려는 경우
-   워크플로가 짧게 실행되며 주로 모델의 응답을 반환하는 것이 목적인 경우

다음과 같은 경우 Agents SDK를 사용하세요.

-   런타임이 턴, 도구 실행, 가드레일, 핸드오프 또는 세션을 관리하기를 원하는 경우
-   에이전트가 아티팩트를 생성하거나 여러 조율된 단계에 걸쳐 동작해야 하는 경우
-   실제 워크스페이스나 [샌드박스 에이전트](sandbox_agents.md)를 통한 재개 가능한 실행이 필요한 경우

둘 중 하나를 전역적으로 선택할 필요는 없습니다. 많은 애플리케이션은 관리형 워크플로에는 SDK를 사용하고, 더 낮은 수준의 경로에는 Responses API를 직접 호출합니다.

## 설치

```bash
pip install openai-agents
```

## Hello world 예제

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_이를 실행하는 경우 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```

## 시작 지점

-   [빠른 시작](quickstart.md)으로 첫 텍스트 기반 에이전트를 구축하세요.
-   그런 다음 [에이전트 실행](running_agents.md#choose-a-memory-strategy)에서 턴 간 상태를 어떻게 유지할지 결정하세요.
-   작업이 실제 파일, 리포지토리 또는 에이전트별 격리된 워크스페이스 상태에 의존한다면 [샌드박스 에이전트 빠른 시작](sandbox_agents.md)을 읽어 보세요.
-   핸드오프와 매니저 스타일 오케스트레이션 중에서 결정하는 중이라면 [에이전트 오케스트레이션](multi_agent.md)을 읽어 보세요.

## 경로 선택

하려는 작업은 알고 있지만 어느 페이지에서 설명하는지 모를 때 이 표를 사용하세요.

| 목표 | 시작 지점 |
| --- | --- |
| 첫 텍스트 에이전트를 만들고 전체 실행 한 번 확인 | [빠른 시작](quickstart.md) |
| 함수 도구, 호스티드 툴 또는 agents as tools 추가 | [도구](tools.md) |
| 실제 격리된 워크스페이스 안에서 코딩, 리뷰 또는 문서 에이전트 실행 | [샌드박스 에이전트 빠른 시작](sandbox_agents.md) 및 [샌드박스 클라이언트](sandbox/clients.md) |
| 핸드오프와 매니저 스타일 오케스트레이션 중에서 결정 | [에이전트 오케스트레이션](multi_agent.md) |
| 턴 간 메모리 유지 | [에이전트 실행](running_agents.md#choose-a-memory-strategy) 및 [세션](sessions/index.md) |
| OpenAI 모델, 웹소켓 전송 또는 비 OpenAI 제공자 사용 | [모델](models/index.md) |
| 출력, 실행 항목, 인터럽션(중단 처리), 재개 상태 검토 | [결과](results.md) |
| `gpt-realtime-2`로 지연 시간이 낮은 음성 에이전트 구축 | [실시간 에이전트 빠른 시작](realtime/quickstart.md) 및 [실시간 전송](realtime/transport.md) |
| 음성-텍스트 변환 / 에이전트 / 텍스트-음성 변환 파이프라인 구축 | [음성 파이프라인 빠른 시작](voice/quickstart.md) |