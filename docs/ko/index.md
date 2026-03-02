---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 매우 적은 추상화로 구성된 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 구축할 수 있게 해줍니다. 이는 이전 에이전트 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비 버전 업그레이드입니다. Agents SDK는 매우 작은 기본 구성 요소 집합을 제공합니다

-   **에이전트**: instructions와 tools가 탑재된 LLM
-   **Agents as tools / 핸드오프**: 에이전트가 특정 작업을 위해 다른 에이전트에 위임할 수 있게 해주는 기능
-   **가드레일**: 에이전트 입력과 출력의 검증을 가능하게 하는 기능

이 기본 구성 요소들은 Python과 결합될 때 도구와 에이전트 간의 복잡한 관계를 표현할 만큼 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있게 해줍니다. 또한 SDK에는 에이전트형 흐름을 시각화하고 디버깅하며, 평가하고, 애플리케이션에 맞춰 모델을 파인튜닝할 수 있게 해주는 내장 **트레이싱**이 포함되어 있습니다.

## Agents SDK 사용 이유

SDK에는 두 가지 핵심 설계 원칙이 있습니다

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 익힐 수 있도록 기본 구성 요소는 적게 유지합니다
2. 기본 설정만으로도 잘 동작하지만, 정확히 어떤 일이 일어날지 직접 맞춤 설정할 수 있습니다

다음은 SDK의 주요 기능입니다

-   **에이전트 루프**: 도구 호출을 처리하고, 결과를 LLM에 다시 전달하며, 작업이 완료될 때까지 계속하는 내장 에이전트 루프
-   **파이썬 우선**: 새로운 추상화를 배울 필요 없이 내장 언어 기능으로 에이전트를 오케스트레이션하고 체이닝
-   **Agents as tools / 핸드오프**: 여러 에이전트 간 작업을 조율하고 위임하는 강력한 메커니즘
-   **가드레일**: 에이전트 실행과 병렬로 입력 검증 및 안전성 점검을 실행하고, 검사를 통과하지 못하면 즉시 실패 처리
-   **함수 도구**: 자동 스키마 생성과 Pydantic 기반 검증으로 모든 Python 함수를 도구로 변환
-   **MCP 서버 도구 호출**: 함수 도구와 동일한 방식으로 동작하는 내장 MCP 서버 도구 통합
-   **세션**: 에이전트 루프 내 작업 컨텍스트 유지를 위한 영속 메모리 계층
-   **휴먼인더루프 (HITL)**: 에이전트 실행 전반에 사람을 참여시키기 위한 내장 메커니즘
-   **트레이싱**: 워크플로우 시각화, 디버깅, 모니터링을 위한 내장 트레이싱과 OpenAI 평가, 파인튜닝, 증류 도구 모음 지원
-   **실시간 에이전트**: 자동 인터럽션(중단 처리) 감지, 컨텍스트 관리, 가드레일 등 기능을 갖춘 강력한 음성 에이전트 구축

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

(_이를 실행하는 경우 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```

## 시작 지점

-   [Quickstart](quickstart.md)로 첫 텍스트 기반 에이전트를 구축하세요
-   그런 다음 [Running agents](running_agents.md#choose-a-memory-strategy)에서 턴 간 상태를 어떻게 유지할지 결정하세요
-   handoffs와 매니저 스타일 오케스트레이션 사이에서 고민 중이라면 [Agent orchestration](multi_agent.md)을 읽어보세요

## 경로 선택

수행할 작업은 알고 있지만 어떤 페이지에 설명되어 있는지 모를 때 이 표를 사용하세요

| 목표 | 시작 지점 |
| --- | --- |
| 첫 텍스트 에이전트를 만들고 하나의 전체 실행을 확인 | [Quickstart](quickstart.md) |
| 함수 도구, 호스티드 툴, 또는 Agents as tools 추가 | [Tools](tools.md) |
| handoffs와 매니저 스타일 오케스트레이션 중 선택 | [Agent orchestration](multi_agent.md) |
| 턴 간 메모리 유지 | [Running agents](running_agents.md#choose-a-memory-strategy) 및 [Sessions](sessions/index.md) |
| OpenAI 모델, websocket 전송, 또는 비 OpenAI 제공자 사용 | [Models](models/index.md) |
| 출력, 실행 항목, 인터럽션(중단 처리), 재개 상태 검토 | [Results](results.md) |
| 저지연 음성 에이전트 구축 | [Realtime agents quickstart](realtime/quickstart.md) |
| speech-to-text / 에이전트 / text-to-speech 파이프라인 구축 | [Voice pipeline quickstart](voice/quickstart.md) |