---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 최소한의 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 구축할 수 있도록 해줍니다. 이는 이전 에이전트 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비가 된 업그레이드입니다. Agents SDK에는 매우 소수의 기본 구성 요소가 있습니다:

-   **에이전트**: instructions 와 tools 를 갖춘 LLM
-   **핸드오프**: 에이전트가 특정 작업을 다른 에이전트에 위임할 수 있게 함
-   **가드레일**: 에이전트 입력과 출력의 유효성 검사를 활성화
-   **세션**: 에이전트 실행 간 대화 이력을 자동으로 관리

Python과 결합하면, 이 기본 구성 요소만으로도 도구와 에이전트 간의 복잡한 관계를 표현할 수 있으며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있습니다. 또한 SDK에는 **트레이싱**이 기본 제공되어 에이전트 플로우를 시각화하고 디버그할 수 있으며, 이를 평가하고 심지어 애플리케이션에 맞춰 모델을 파인튜닝할 수도 있습니다.

## Agents SDK 사용 이유

SDK는 두 가지 설계 원칙을 따릅니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 학습할 수 있도록 기본 구성 요소는 최소화
2. 기본 설정만으로도 잘 작동하지만, 동작을 원하는 대로 세밀하게 커스터마이즈 가능

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 완료될 때까지 반복을 처리하는 내장 루프
-   Python 우선: 새로운 추상화를 배울 필요 없이 언어의 기본 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 다수의 에이전트를 조정하고 위임하는 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 점검을 실행하고, 실패 시 조기 중단
-   세션: 에이전트 실행 간 대화 이력을 자동 관리하여 수동 상태 관리를 제거
-   함수 도구: 어떤 Python 함수든 도구로 변환, 자동 스키마 생성과 Pydantic 기반 검증 제공
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있으며 OpenAI의 평가, 파인튜닝, 증류 도구도 활용 가능

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