---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 최소한의 추상화로 가볍고 사용하기 쉬운 패키지에서 에이전트형 AI 앱을 구축할 수 있게 해줍니다. 이는 이전 에이전트 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비가 된 업그레이드입니다. Agents SDK에는 매우 소수의 기본 컴포넌트가 있습니다:

-   **에이전트**: instructions와 tools를 갖춘 LLM
-   **핸드오프**: 특정 작업을 위해 에이전트가 다른 에이전트에 위임하도록 함
-   **가드레일**: 에이전트 입력과 출력의 유효성을 검증하도록 함
-   **세션**: 에이전트 실행 간 대화 기록을 자동으로 유지함

파이썬과 함께 사용할 때, 이 기본 컴포넌트들은 도구와 에이전트 간의 복잡한 관계를 표현하기에 충분히 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 구축할 수 있게 합니다. 또한, SDK에는 에이전트 플로우를 시각화하고 디버깅하며, 평가하고 애플리케이션을 위한 모델을 파인튜닝할 수 있게 해주는 기본 제공 **트레이싱**이 포함되어 있습니다.

## Agents SDK 사용 이유

이 SDK는 두 가지 핵심 설계 원칙이 있습니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 학습이 빠르도록 기본 컴포넌트는 최소화합니다
2. 기본 설정으로도 훌륭하게 동작하지만, 발생하는 과정을 정확히 커스터마이즈할 수 있습니다

SDK의 주요 기능은 다음과 같습니다:

-   에이전트 루프: 도구 호출, 결과를 LLM에 전달, LLM이 완료될 때까지 루프를 처리하는 기본 제공 에이전트 루프
-   파이썬 우선: 새로운 추상화를 배우지 않고도 언어의 기본 기능으로 에이전트를 오케스트레이션하고 체이닝
-   핸드오프: 여러 에이전트 간 조정과 위임을 위한 강력한 기능
-   가드레일: 에이전트와 병렬로 입력 유효성 검사와 체크를 실행하고, 실패 시 빠르게 중단
-   세션: 에이전트 실행 간 대화 기록을 자동으로 관리하여 수동 상태 관리 제거
-   함수 도구: 어떤 Python 함수든 자동 스키마 생성과 Pydantic 기반 검증을 통해 도구로 전환
-   트레이싱: 워크플로를 시각화, 디버그, 모니터링할 수 있는 기본 제공 트레이싱과 OpenAI의 평가, 파인튜닝, 지식 증류 도구 모음 연계

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

(_이 코드를 실행하려면 `OPENAI_API_KEY` 환경 변수를 설정하세요_)

```bash
export OPENAI_API_KEY=sk-...
```