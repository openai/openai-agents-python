---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 추상화를 최소화한 가볍고 사용하기 쉬운 패키지로 에이전트형 AI 앱을 구축할 수 있게 해줍니다. 이는 이전 에이전트 실험 프로젝트인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션급 업그레이드입니다. Agents SDK는 매우 작은 범위의 기본 구성요소를 제공합니다:

-   **에이전트**: 지시문(instructions)과 도구(tools)를 갖춘 LLM
-   **핸드오프**: 특정 작업을 다른 에이전트에 위임할 수 있도록 함
-   **가드레일**: 에이전트의 입력과 출력에 대한 검증을 가능하게 함
-   **세션**: 에이전트 실행 간 대화 이력을 자동으로 유지 관리함

Python과 결합하면, 이 기본 구성요소만으로도 도구와 에이전트 간의 복잡한 관계를 충분히 표현할 수 있으며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있습니다. 또한 SDK에는 에이전트 플로우를 시각화하고 디버깅할 수 있게 해주는 내장 **트레이싱**이 포함되어 있으며, 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝하는 데에도 활용할 수 있습니다.

## Agents SDK를 사용하는 이유

SDK의 설계 원칙은 다음 두 가지입니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 배울 수 있도록 기본 구성요소는 최소화합니다
2. 기본 설정만으로도 훌륭하게 작동하지만, 원하는 동작을 정확히 커스터마이즈할 수 있습니다

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 완료될 때까지 루프를 처리하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 배우지 않고도 언어의 기본 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간 조정과 위임을 위한 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 점검을 실행하고, 실패 시 빠르게 중단
-   세션: 에이전트 실행 간 대화 이력을 자동으로 관리하여 수동 상태 관리를 제거
-   함수 도구: 어떤 Python 함수든 자동 스키마 생성과 Pydantic 기반 검증으로 도구로 변환
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있는 내장 트레이싱과 OpenAI 평가, 파인튜닝, 증류 도구 연동

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

(_이 코드를 실행할 때는 `OPENAI_API_KEY` 환경 변수를 설정해야 합니다_)

```bash
export OPENAI_API_KEY=sk-...
```