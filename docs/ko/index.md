---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 가벼우면서도 사용하기 쉬운 패키지로, 최소한의 추상화만으로 에이전트형 AI 앱을 구축할 수 있게 해줍니다. 이는 이전의 에이전트 실험 프로젝트인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션급 업그레이드입니다. Agents SDK는 매우 소수의 기본 구성 요소(basic components)만 제공합니다:

- **에이전트**: instructions와 tools를 갖춘 LLM
- **핸드오프**: 특정 작업을 다른 에이전트에 위임할 수 있도록 함
- **가드레일**: 에이전트의 입력과 출력을 검증할 수 있도록 함
- **세션**: 에이전트 실행 전반에 걸쳐 대화 이력을 자동으로 관리

파이썬과 결합하면, 이러한 기본 구성 요소만으로도 도구와 에이전트 간의 복잡한 관계를 충분히 표현할 수 있고, 높은 학습 곡선 없이 실사용 애플리케이션을 만들 수 있습니다. 또한, SDK에는 에이전트 플로우를 시각화하고 디버깅할 수 있는 내장 **트레이싱**이 포함되어 있으며, 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝하는 데에도 활용할 수 있습니다.

## Why use the Agents SDK

SDK는 다음의 두 가지 설계 원칙을 따릅니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 학습할 수 있도록 기본 구성 요소는 최소화합니다
2. 기본값만으로도 잘 동작하되, 실제로 어떤 일이 일어나는지 정확히 커스터마이즈할 수 있습니다

SDK의 주요 기능은 다음과 같습니다:

- 에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 완료될 때까지의 루프를 처리하는 내장 에이전트 루프
- 파이썬 우선: 새로운 추상화를 학습할 필요 없이, 내장 언어 기능만으로 에이전트를 오케스트레이션하고 체이닝
- 핸드오프: 여러 에이전트 간을 조정하고 위임할 수 있는 강력한 기능
- 가드레일: 에이전트와 병렬로 입력 검증과 체크를 실행하고, 실패 시 조기 중단
- 세션: 에이전트 실행 전반의 대화 이력을 자동 관리하여 수동 상태 관리를 제거
- 함수 도구: 어떤 Python 함수든 도구로 전환, 자동 스키마 생성과 Pydantic 기반 검증 지원
- 트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있는 내장 트레이싱과 OpenAI의 평가, 파인튜닝, 증류 도구 활용

## Installation

```bash
pip install openai-agents
```

## Hello world example

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_If running this, ensure you set the `OPENAI_API_KEY` environment variable_)

```bash
export OPENAI_API_KEY=sk-...
```