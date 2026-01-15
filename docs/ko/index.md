---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 소수의 추상화만으로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 구축할 수 있도록 합니다. 이는 이전 에이전트 실험 프레임워크인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 레디 업그레이드입니다. Agents SDK에는 매우 작은 기본 구성 요소 집합이 있습니다:

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **핸드오프**: 에이전트가 특정 작업을 다른 에이전트에 위임할 수 있게 함
-   **가드레일**: 에이전트 입력 및 출력의 검증을 가능하게 함
-   **세션**: 에이전트 실행 전반에서 대화 기록을 자동으로 유지 관리함

Python과 결합하면, 이 기본 구성 요소들은 도구와 에이전트 간의 복잡한 관계를 표현할 만큼 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있게 합니다. 또한 SDK에는 에이전트 흐름을 시각화하고 디버그할 수 있는 내장 **트레이싱**이 포함되어 있어, 이를 평가하고 심지어 애플리케이션에 맞게 모델을 파인튜닝할 수도 있습니다.

## Agents SDK를 사용하는 이유

SDK의 설계 원칙은 두 가지입니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 배울 수 있을 만큼 기본 구성 요소는 적게 유지
2. 기본 설정만으로도 훌륭히 작동하지만, 동작을 정확히 원하는 대로 커스터마이즈 가능

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 완료될 때까지 루프를 처리하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 학습할 필요 없이, 내장 언어 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간의 조정과 위임을 위한 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 점검을 실행하며, 점검이 실패하면 조기에 중단
-   세션: 에이전트 실행 전반의 대화 기록을 자동 관리하여 수동 상태 처리를 제거
-   함수 도구: 어떤 Python 함수든 도구로 전환, 자동 스키마 생성과 Pydantic 기반 검증 제공
-   트레이싱: 워크플로를 시 visualize, 디버그, 모니터링할 수 있는 내장 트레이싱과 함께 OpenAI의 평가, 파인튜닝, 디스틸레이션 도구 활용

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

(_실행 시 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```