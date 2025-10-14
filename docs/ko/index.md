---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 적은 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 만들 수 있게 해줍니다. 이는 이전의 에이전트 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비 업그레이드입니다. Agents SDK 는 매우 작은 기본 구성요소 세트를 제공합니다:

-   **에이전트**: instructions 와 tools 를 갖춘 LLM
-   **핸드오프**: 에이전트가 특정 작업을 다른 에이전트에 위임할 수 있게 함
-   **가드레일**: 에이전트 입력과 출력을 검증할 수 있게 함
-   **세션**: 에이전트 실행 전반에 걸친 대화 기록을 자동으로 유지 관리

Python 과 결합하면, 이 기본 구성요소만으로도 도구와 에이전트 간의 복잡한 관계를 표현하고 가파른 학습 곡선 없이 실사용 애플리케이션을 구축할 수 있습니다. 또한 SDK 는 내장된 **트레이싱**을 제공하여 에이전트 플로를 시각화하고 디버그하며, 평가하고 심지어 애플리케이션에 맞게 모델을 파인튜닝할 수 있습니다.

## Agents SDK 사용 이유

SDK 의 설계 원칙은 두 가지입니다:

1. 쓸 만큼 충분한 기능을 제공하되, 빠르게 익힐 수 있도록 기본 구성요소는 최소화
2. 기본 설정으로도 훌륭히 동작하지만, 동작 방식을 세밀하게 커스터마이즈 가능

주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM 에 전달, LLM 이 완료될 때까지 루프를 처리하는 내장 에이전트 루프
-   파이썬 우선: 새로운 추상화를 배울 필요 없이, 내장 언어 기능으로 에이전트를 오케스트레이션하고 연결
-   핸드오프: 여러 에이전트 간 조정과 위임을 위한 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 검증과 체크를 실행하고, 실패 시 조기 중단
-   세션: 에이전트 실행 전반의 대화 이력을 자동 관리하여 수동 상태 관리를 제거
-   함수 도구: 어떤 Python 함수든 도구로 전환하고, 스키마 자동 생성과 Pydantic 기반 검증 제공
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있는 내장 트레이싱과 OpenAI 의 평가, 파인튜닝, 증류 도구 제품군 활용

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

(_이 코드를 실행할 경우, `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```