---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 최소한의 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전트 기반 AI 앱을 구축할 수 있게 해줍니다. 이는 에이전트에 대한 이전 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비 업그레이드입니다. Agents SDK에는 매우 작은 범위의 기본 구성 요소가 있습니다:

-   **에이전트**: instructions 와 tools 를 갖춘 LLM
-   **핸드오프**: 에이전트가 특정 작업을 다른 에이전트에게 위임할 수 있도록 함
-   **가드레일**: 에이전트 입력과 출력의 유효성을 검증하도록 함
-   **세션**: 에이전트 실행 간 대화 히스토리를 자동으로 유지

Python과 결합하면, 이 기본 구성 요소만으로도 도구와 에이전트 간의 복잡한 관계를 표현할 수 있으며, 가파른 학습 곡선 없이 실제 애플리케이션을 만들 수 있습니다. 또한 SDK에는 에이전트 플로우를 시각화하고 디버그하며, 평가하고 심지어 애플리케이션에 맞게 모델을 파인튜닝할 수 있게 해주는 내장 **트레이싱**이 포함되어 있습니다.

## Agents SDK를 사용하는 이유

SDK는 두 가지 핵심 설계 원칙을 따릅니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 익힐 수 있도록 기본 구성 요소는 최소화합니다
2. 기본 설정만으로도 잘 동작하지만, 동작을 정확히 원하는 대로 커스터마이즈할 수 있습니다

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: tools 호출, 결과를 LLM에 전달, LLM이 완료될 때까지 루프를 수행하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 배울 필요 없이 언어의 내장 기능을 사용해 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간의 조율과 위임을 가능하게 하는 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 체크를 실행하고, 실패 시 조기에 중단
-   세션: 에이전트 실행 전반에 걸친 대화 히스토리 자동 관리로 수동 상태 관리 제거
-   함수 도구: 어떤 Python 함수든 도구로 변환하고, 스키마 자동 생성과 Pydantic 기반 검증 제공
-   트레이싱: 워크플로우를 시각화, 디버그, 모니터링하고 OpenAI 평가, 파인튜닝, 증류 도구 제품군을 사용할 수 있게 해주는 내장 트레이싱

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

(_실행하는 경우 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```