---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 최소한의 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전트 기반 AI 앱을 구축할 수 있도록 합니다. 이는 이전 에이전트 실험 프로젝트인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비 완료 업그레이드입니다. Agents SDK에는 매우 소수의 기본 컴포넌트가 있습니다:

-   **에이전트**: instructions와 도구를 갖춘 LLM
-   **핸드오프**: 특정 작업에 대해 다른 에이전트에 위임할 수 있게 함
-   **가드레일**: 에이전트 입력과 출력의 유효성 검증을 가능하게 함
-   **세션**: 에이전트 실행 전반에 걸쳐 대화 이력을 자동으로 유지 관리함

파이썬과 결합하면, 이러한 기본 컴포넌트만으로도 도구와 에이전트 간의 복잡한 관계를 표현할 수 있으며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있습니다. 또한 SDK에는 에이전트 플로우를 시각화하고 디버그하며, 평가하고 심지어 애플리케이션에 맞게 모델을 미세 튜닝할 수 있는 내장 **트레이싱**이 포함되어 있습니다.

## Why use the Agents SDK

SDK의 두 가지 핵심 설계 원칙은 다음과 같습니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 학습할 수 있도록 기본 컴포넌트는 최소화합니다
2. 기본 설정만으로도 잘 동작하지만, 동작을 정확히 원하는 대로 커스터마이즈할 수 있습니다

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM 종료 시점까지의 루프를 처리하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 학습할 필요 없이, 언어의 기본 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간의 조정과 위임을 위한 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 점검을 수행하고, 실패 시 조기 중단
-   세션: 에이전트 실행 전반의 대화 이력을 자동으로 관리하여 수동 상태 관리를 제거
-   함수 도구: 어떤 파이썬 함수든 자동 스키마 생성과 Pydantic 기반 검증으로 도구로 전환
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링하고 OpenAI의 평가, 파인튜닝, 증류 도구를 활용할 수 있는 내장 트레이싱

## Installation

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

(_이를 실행하려면 `OPENAI_API_KEY` 환경 변수를 설정해야 합니다_)

```bash
export OPENAI_API_KEY=sk-...
```