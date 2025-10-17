---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 소수의 추상화만으로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 만들 수 있게 해줍니다. 이는 이전 에이전트 실험작인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션급 업그레이드입니다. Agents SDK는 아주 소수의 기본 구성요소만을 제공합니다:

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **핸드오프**: 특정 작업을 다른 에이전트에게 위임할 수 있게 함
-   **가드레일**: 에이전트 입력과 출력의 유효성을 검증할 수 있게 함
-   **세션**: 에이전트 실행 간 대화 이력을 자동으로 유지 관리함

이러한 구성요소는 Python과 결합되어 도구와 에이전트 간의 복잡한 관계를 표현할 만큼 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있게 해줍니다. 또한 SDK에는 에이전트 플로우를 시각화하고 디버깅할 수 있는 내장 **트레이싱**이 포함되어 있으며, 이를 평가하고 애플리케이션에 맞춰 모델을 파인튜닝하는 것까지 지원합니다.

## Agents SDK 사용 이유

SDK는 두 가지 설계 원칙을 따릅니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 배울 수 있도록 구성요소는 최소화합니다
2. 기본 설정만으로도 훌륭하게 작동하지만, 동작을 정확히 원하는 대로 커스터마이즈할 수 있습니다

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 완료될 때까지 반복을 처리하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 배울 필요 없이, 언어의 기본 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간의 조정과 위임을 가능하게 하는 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 점검을 수행하고, 실패 시 조기에 중단
-   세션: 에이전트 실행 간 대화 이력을 자동으로 관리하여 수동 상태 관리를 제거
-   함수 도구: 모든 Python 함수를 도구로 변환하고, 스키마 자동 생성과 Pydantic 기반 검증 제공
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있는 내장 트레이싱과 OpenAI 제품군의 평가, 파인튜닝 및 디스틸레이션 도구 연동

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