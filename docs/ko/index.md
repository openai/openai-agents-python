---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 최소한의 추상화로 가벼우면서도 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 만들 수 있게 해줍니다. 이는 이전 에이전트 실험 프로젝트인 [Swarm](https://github.com/openai/swarm/tree/main)을 프로덕션에 적합하도록 업그레이드한 것입니다. Agents SDK 는 매우 적은 수의 기본 구성 요소를 제공합니다:

- **에이전트**: instructions 와 tools 를 갖춘 LLM
- **핸드오프**: 특정 작업에 대해 에이전트가 다른 에이전트에 위임할 수 있게 함
- **가드레일**: 에이전트 입력과 출력을 검증 가능하게 함
- **세션**: 에이전트 실행 전반에 걸쳐 대화 내역을 자동으로 유지 관리

파이썬과 결합하면, 이 기본 구성 요소만으로도 도구와 에이전트 간 복잡한 관계를 표현할 수 있으며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있습니다. 또한 이 SDK에는 에이전트 플로우를 시각화하고 디버그할 수 있는 **트레이싱**이 내장되어 있어 평가하고, 심지어 애플리케이션에 맞춰 모델을 파인튜닝할 수도 있습니다.

## Agents SDK 사용 이유

이 SDK 의 설계 원칙은 다음 두 가지입니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 배울 수 있도록 기본 구성 요소는 최소화
2. 기본 설정만으로도 훌륭히 동작하지만, 동작 방식을 정확히 원하는 대로 커스터마이즈 가능

SDK 의 주요 기능은 다음과 같습니다:

- Agent loop: 도구 호출, 결과를 LLM 에 전달, LLM 이 완료될 때까지 반복을 처리하는 루프가 내장
- 파이썬 우선: 새로운 추상화를 배울 필요 없이, 언어의 기본 기능만으로 에이전트를 오케스트레이션하고 체이닝
- 핸드오프: 여러 에이전트 간 조정과 위임을 가능하게 하는 강력한 기능
- 가드레일: 에이전트와 병렬로 입력 검증과 점검을 실행하며, 점검이 실패하면 조기 중단
- 세션: 에이전트 실행 간 대화 내역을 자동으로 관리하여 수동 상태 관리 제거
- 함수 도구: 어떤 Python 함수든 자동 스키마 생성과 Pydantic 기반 검증으로 도구로 전환
- 트레이싱: 워크플로를 시각화, 디버그, 모니터링하고 OpenAI 제품군의 평가, 파인튜닝 및 지식 증류 도구를 활용할 수 있게 해주는 기능 내장

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

(_실행 시 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```