---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 매우 적은 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전틱 AI 앱을 구축할 수 있게 합니다. 이는 에이전트에 대한 이전 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션급 업그레이드입니다. Agents SDK는 매우 작은 범위의 기본 구성 요소를 제공합니다:

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **핸드오프**: 특정 작업에 대해 에이전트가 다른 에이전트에 위임할 수 있게 함
-   **가드레일**: 에이전트 입력과 출력을 검증할 수 있게 함
-   **세션**: 에이전트 실행 전반에 걸쳐 대화 기록을 자동으로 관리함

Python과 결합하면, 이러한 기본 구성 요소만으로도 도구와 에이전트 간의 복잡한 관계를 표현할 수 있으며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있습니다. 또한, SDK에는 에이전트 흐름을 시각화하고 디버깅할 수 있는 내장 **트레이싱**이 포함되어 있고, 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝하는 것까지 지원합니다.

## Agents SDK 사용 이유

SDK의 주요 설계 원칙은 다음과 같습니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 익힐 수 있도록 기본 구성 요소는 최소화할 것
2. 기본 설정만으로도 훌륭하게 동작하되, 동작 방식을 정확히 원하는 대로 커스터마이즈할 수 있을 것

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 종료 신호를 낼 때까지 루핑을 처리하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 배울 필요 없이, 내장 언어 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간 조정 및 위임을 위한 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 점검을 실행하며, 실패 시 조기 중단
-   세션: 에이전트 실행 간 자동 대화 기록 관리로 수동 상태 관리를 제거
-   함수 도구: 어떤 Python 함수든 도구로 바꾸고, 스키마 자동 생성 및 Pydantic 기반 검증 제공
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있는 내장 트레이싱과 더불어 OpenAI의 평가, 파인튜닝, 지식 증류 도구 제품군 활용

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

(_이 코드를 실행한다면 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```