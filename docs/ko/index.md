---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 매우 적은 추상화만으로, 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 구축할 수 있게 해줍니다. 이는 이전 에이전트 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비 완료 업그레이드 버전입니다. Agents SDK는 매우 작은 기본 구성 요소 집합을 제공합니다:

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **Agents as tools / 핸드오프**: 에이전트가 특정 작업을 위해 다른 에이전트에 위임할 수 있도록 하는 기능
-   **가드레일**: 에이전트 입력과 출력의 검증을 가능하게 하는 기능

이 기본 구성 요소들은 Python과 결합될 때 도구와 에이전트 사이의 복잡한 관계를 표현할 만큼 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있게 해줍니다. 또한 SDK에는 에이전트형 흐름을 시각화하고 디버깅할 수 있으며, 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝까지 할 수 있는 내장 **트레이싱**이 포함되어 있습니다.

## Agents SDK 사용 이유

SDK에는 두 가지 핵심 설계 원칙이 있습니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 학습할 수 있을 만큼 기본 구성 요소는 적게 유지
2. 기본 설정만으로도 잘 동작하지만, 정확히 어떤 일이 일어날지 원하는 대로 사용자 지정 가능

다음은 SDK의 주요 기능입니다:

-   **에이전트 루프**: 도구 호출을 처리하고, 결과를 LLM에 다시 전달하며, 작업이 완료될 때까지 계속하는 내장 에이전트 루프
-   **파이썬 우선**: 새로운 추상화를 배울 필요 없이, 내장 언어 기능으로 에이전트 오케스트레이션 및 체이닝 수행
-   **Agents as tools / 핸드오프**: 여러 에이전트 간 작업을 조정하고 위임하는 강력한 메커니즘
-   **가드레일**: 에이전트 실행과 병렬로 입력 검증 및 안전성 검사를 수행하고, 검사를 통과하지 못하면 빠르게 실패 처리
-   **함수 도구**: 자동 스키마 생성과 Pydantic 기반 검증으로 임의의 Python 함수를 도구로 변환
-   **MCP 서버 도구 호출**: 함수 도구와 동일한 방식으로 동작하는 내장 MCP 서버 도구 통합
-   **세션**: 에이전트 루프 내 작업 컨텍스트를 유지하기 위한 영속 메모리 계층
-   **휴먼인더루프 (HITL)**: 에이전트 실행 전반에서 사람을 참여시키는 내장 메커니즘
-   **트레이싱**: 워크플로 시각화, 디버깅, 모니터링을 위한 내장 트레이싱과 OpenAI 평가, 파인튜닝, 증류 도구 모음 지원
-   **실시간 에이전트**: 자동 인터럽션(중단 처리) 감지, 컨텍스트 관리, 가드레일 등의 기능으로 강력한 음성 에이전트 구축

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

(_이 예제를 실행하는 경우 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```

## 시작 지점

-   [Quickstart](quickstart.md)로 첫 번째 텍스트 기반 에이전트를 구축하세요
-   [Realtime agents quickstart](realtime/quickstart.md)로 저지연 음성 에이전트를 구축하세요
-   대신 speech-to-text / 에이전트 / text-to-speech 파이프라인을 원한다면 [Voice pipeline quickstart](voice/quickstart.md)를 참조하세요