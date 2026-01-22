---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)를 사용하면 추상화가 거의 없는 가볍고 사용하기 쉬운 패키지로 에이전트형 AI 앱을 만들 수 있습니다. 이는 에이전트를 위한 이전 실험인 [Swarm](https://github.com/openai/swarm/tree/main)의 프로덕션 준비 완료 업그레이드 버전입니다. Agents SDK에는 매우 작은 기본 구성 요소(primitives) 집합이 있습니다:

-   **에이전트**, 즉 instructions 와 tools 를 갖춘 LLM
-   **Agents as tools / 핸드오프**, 특정 작업을 위해 에이전트가 다른 에이전트에게 위임할 수 있게 해주는 기능
-   **가드레일**, 에이전트 입력과 출력의 검증을 가능하게 하는 기능

이러한 기본 구성 요소는 Python과 결합될 때 도구와 에이전트 간의 복잡한 관계를 표현하기에 충분히 강력하며, 가파른 학습 곡선 없이 실제 애플리케이션을 만들 수 있게 해줍니다. 또한 SDK에는 내장된 **트레이싱**이 포함되어 있어 에이전트형 플로우를 시각화하고 디버그할 수 있으며, 이를 평가하고 애플리케이션을 위해 모델을 미세 조정하는 것까지 가능합니다.

## Agents SDK 사용 이유

SDK에는 두 가지 핵심 설계 원칙이 있습니다:

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하되, 빠르게 학습할 수 있도록 기본 구성 요소는 충분히 적게 유지합니다
2. 기본 설정만으로도 훌륭하게 동작하지만, 정확히 어떤 일이 일어나는지 원하는 대로 커스터마이즈할 수 있습니다

다음은 SDK의 주요 기능입니다:

-   **에이전트 루프**: 도구 호출을 처리하고 결과를 LLM에 다시 전달한 뒤 작업이 완료될 때까지 계속하는 내장 에이전트 루프입니다
-   **파이썬 우선**: 새로운 추상화를 배울 필요 없이 내장 언어 기능을 사용해 에이전트를 오케스트레이션하고 체이닝합니다
-   **Agents as tools / 핸드오프**: 여러 에이전트에 걸친 작업의 조정과 위임을 위한 강력한 메커니즘입니다
-   **가드레일**: 에이전트 실행과 병렬로 입력 검증과 안전성 체크를 수행하며, 체크를 통과하지 못하면 즉시 실패 처리합니다
-   **함수 도구**: 자동 스키마 생성과 Pydantic 기반 검증을 통해 어떤 Python 함수든 도구로 전환합니다
-   **MCP 서버 도구 호출**: 함수 도구와 동일한 방식으로 동작하는 내장 MCP 서버 도구 통합입니다
-   **세션**: 에이전트 루프 내에서 작업 컨텍스트를 유지하기 위한 영속 메모리 레이어입니다
-   **휴먼인더루프 (HITL)**: 에이전트 실행 전반에 걸쳐 사람을 참여시키기 위한 내장 메커니즘입니다
-   **트레이싱**: 워크플로를 시각화, 디버깅, 모니터링하기 위한 내장 트레이싱으로, OpenAI 평가, 미세 조정, 디스틸레이션 도구 제품군을 지원합니다
-   **실시간 에이전트**: 자동 인터럽션(중단 처리) 감지, 컨텍스트 관리, 가드레일 등 기능을 갖춘 강력한 음성 에이전트를 구축합니다

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

(_이를 실행하는 경우 `OPENAI_API_KEY` 환경 변수를 설정했는지 확인하세요_)

```bash
export OPENAI_API_KEY=sk-...
```