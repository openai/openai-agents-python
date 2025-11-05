---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 매우 적은 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 빌드할 수 있게 해줍니다. 이는 이전 에이전트 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션급 업그레이드입니다. Agents SDK는 매우 작은 기본 구성 요소 집합을 제공합니다:

- **에이전트**: instructions 와 tools 를 갖춘 LLM
- **핸드오프**: 특정 작업을 위해 에이전트가 다른 에이전트에 위임할 수 있도록 함
- **가드레일**: 에이전트 입력과 출력의 유효성 검증을 가능하게 함
- **세션**: 에이전트 실행 간 대화 내역을 자동으로 유지함

Python 과 결합하면, 이 기본 구성 요소들은 도구와 에이전트 간의 복잡한 관계를 표현하기에 충분히 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있게 해줍니다. 또한, SDK에는 에이전트 플로우를 시각화하고 디버그할 수 있는 기본 제공 **트레이싱**이 포함되어 있으며, 이를 평가하고 애플리케이션에 맞게 모델을 파인튜닝하는 것까지 지원합니다.

## Agents SDK 사용 이유

이 SDK의 설계 원칙은 두 가지입니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 배울 수 있을 만큼 기본 구성 요소는 적게
2. 기본 설정만으로도 훌륭하게 동작하되, 동작을 정확히 원하는 대로 커스터마이즈 가능하게

SDK의 주요 기능은 다음과 같습니다:

- 에이전트 루프: 도구 호출, 결과를 LLM으로 전달, LLM 완료 시점까지 루프를 처리하는 기본 제공 루프
- 파이썬 우선: 새로운 추상화를 배울 필요 없이, 언어의 기본 기능으로 에이전트를 오케스트레이션하고 체이닝
- 핸드오프: 여러 에이전트 간 조율과 위임을 가능하게 하는 강력한 기능
- 가드레일: 에이전트와 병렬로 입력 검증과 점검을 수행하고 실패 시 조기에 중단
- 세션: 에이전트 실행 전반에서 대화 내역을 자동 관리하여 수동 상태 관리 제거
- 함수 도구: 모든 Python 함수를 도구로 변환, 자동 스키마 생성과 Pydantic 기반 검증 제공
- 트레이싱: 워크플로를 시각화, 디버그, 모니터링하고 OpenAI의 평가, 파인튜닝, 증류 도구를 활용할 수 있는 기본 제공 트레이싱

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

(_이 예제를 실행하려면 `OPENAI_API_KEY` 환경 변수를 설정해야 합니다_)

```bash
export OPENAI_API_KEY=sk-...
```