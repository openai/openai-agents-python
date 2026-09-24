---
search:
  exclude: true
---
# OpenAI Agents SDK

!!! note "중요 공지"

    Agents SDK는 **기능 개발이 완료된 상태**입니다. 유지 관리, 보안 수정, 치명적 버그 수정 및 호환성 관련 작업은 계속되지만, 주요 신규 기능은 계획되어 있지 않습니다. 새로운 에이전트 애플리케이션에는 관리형 Codex 하네스에서 실행되는 **[Agents API](https://developers.openai.com/api/docs/guides/agents-api/quickstart)**를 권장합니다.

    기존 애플리케이션에서는 Agents SDK를 계속 사용할 수 있습니다. Agents API가 아직 지원하지 않는 기능이 필요한 신규 애플리케이션의 경우, SDK는 단기적인 선택지가 될 수 있습니다.

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)는 애플리케이션 코드에서 에이전트 워크플로를 구축하기 위한 오픈 소스 프레임워크입니다. [Swarm](https://github.com/openai/swarm/tree/main)을 기반으로 하며, 가드레일과 기본 제공 트레이싱을 통해 경량 멀티 에이전트 오케스트레이션을 프로덕션 애플리케이션에 도입합니다.

SDK는 애플리케이션에서 에이전트 루프를 실행하고 MCP 서버 도구를 포함한 모델 호출과 도구 실행을 조정합니다. 세션은 여러 실행에 걸쳐 대화 컨텍스트를 유지하며, 사람의 승인을 통해 애플리케이션이 검토를 위해 도구 실행을 일시 중지할 수 있습니다. 핵심 기본 구성요소는 다음과 같습니다.

-   **에이전트**: 지침과 도구를 갖춘 LLM
-   **Agents as tools / 핸드오프**: 에이전트가 특정 작업을 다른 에이전트에게 위임할 수 있도록 하는 기능
-   **가드레일**: 에이전트 입력과 출력의 검증을 지원하는 기능

이러한 기본 구성요소를 Python과 결합하여 여러 단계로 구성된 워크플로를 조정할 수 있습니다. 기본 제공 **트레이싱**을 사용하면 이러한 워크플로를 시각화하고 디버깅하며 평가할 수 있습니다. SDK는 지연 시간이 짧은 음성 상호작용을 위한 [실시간 에이전트](realtime/guide.md)와 파일 및 명령으로 작업하기 위한 [샌드박스 에이전트](sandbox_agents.md)도 지원합니다.

## Agents SDK 사용 이유 {#why-use-the-agents-sdk}

SDK에는 다음과 같은 두 가지 핵심 설계 원칙이 있습니다.

1. 사용할 가치가 있을 만큼 충분한 기능을 제공하면서도, 빠르게 익힐 수 있도록 기본 구성요소의 수를 최소화합니다.
2. 별도의 설정 없이도 원활하게 작동하면서, 동작을 원하는 대로 세밀하게 맞춤 설정할 수 있습니다.

SDK의 주요 기능은 다음과 같습니다.

-   **에이전트**: 지침, 도구, 가드레일, 핸드오프와 작업이 완료될 때까지 계속 실행되는 기본 제공 루프를 사용해 에이전트를 구축합니다.
-   **샌드박스 에이전트**: 실제 격리된 워크스페이스에서 전문 에이전트를 실행합니다. 샌드박스 에이전트는 매니페스트로 정의된 파일, 샌드박스 클라이언트 선택 및 재개 가능한 샌드박스 세션을 지원합니다.
-   **실시간 에이전트**: `gpt-realtime-2.1`, 자동 인터럽션(중단 처리) 감지, 컨텍스트 관리, 가드레일 등을 활용해 강력한 음성 에이전트를 구축합니다.
-   **음성 에이전트**: 음성 텍스트 변환, 에이전트 워크플로 및 텍스트 음성 변환을 결합한 음성 파이프라인을 구축합니다.
-   **파이썬 우선**: 새로운 추상화를 학습할 필요 없이 기본 제공 언어 기능을 사용하여 에이전트를 오케스트레이션하고 연결합니다.
-   **Agents as tools / 핸드오프**: 여러 에이전트 간에 작업을 조정하고 위임하기 위한 강력한 메커니즘입니다.
-   **가드레일**: 에이전트 실행과 동시에 입력 검증 및 안전 검사를 병렬로 수행하고, 검사를 통과하지 못하면 즉시 실패 처리합니다.
-   **함수 도구**: 자동 스키마 생성과 Pydantic 기반 검증을 통해 모든 Python 함수를 도구로 변환합니다.
-   **MCP 서버 도구 호출**: 원격 MCP 도구를 함수 도구와 함께 에이전트에 제공하는 기본 제공 통합 기능입니다.
-   **세션**: 에이전트 루프 내에서 작업 컨텍스트를 유지하기 위한 영구 메모리 계층입니다.
-   **휴먼인더루프 (HITL)**: 에이전트 실행 중 사람을 참여시키기 위한 기본 제공 메커니즘입니다.
-   **트레이싱**: 워크플로를 시각화하고 디버깅하며 모니터링하기 위한 기본 제공 트레이싱으로, OpenAI의 평가, 미세 조정 및 증류 도구 모음을 지원합니다.

## Agents SDK와 Responses API 비교 {#agents-sdk-or-responses-api}

새로운 에이전트 애플리케이션은 [Agents API](https://developers.openai.com/api/docs/guides/agents-api/quickstart)로 시작하는 것이 좋습니다. 아래 비교는 자체 애플리케이션 코드에서 에이전트 워크플로를 실행해야 하는 경우에 적용됩니다.

SDK는 OpenAI 모델에 기본적으로 Responses API를 사용하지만, 모델 호출을 더 높은 수준의 런타임으로 래핑합니다.

다음과 같은 경우 Responses API를 직접 사용합니다.

-   루프, 도구 디스패치 및 상태 처리를 직접 관리하려는 경우
-   워크플로의 실행 시간이 짧고 주된 목적이 모델 응답을 반환하는 것인 경우

다음과 같은 경우 Agents SDK를 사용합니다.

-   런타임에서 턴, 도구 실행, 가드레일, 핸드오프 또는 세션을 관리하도록 하려는 경우
-   에이전트가 결과물을 생성하거나 조정된 여러 단계에 걸쳐 동작해야 하는 경우
-   [샌드박스 에이전트](sandbox_agents.md)를 통해 실제 워크스페이스 또는 재개 가능한 실행이 필요한 경우

전체 애플리케이션에 하나만 선택할 필요는 없습니다. 많은 애플리케이션에서 관리형 워크플로에는 SDK를 사용하고, 더 낮은 수준의 실행 경로에는 Responses API를 직접 호출합니다.

## 설치 {#installation}

```bash
pip install openai-agents
```

## Hello world 예제 {#hello-world-example}

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_이를 실행하려면 `OPENAI_API_KEY` 환경 변수를 설정해야 합니다_)

```bash
export OPENAI_API_KEY=sk-...
```

## 시작 지점 {#start-here}

-   [빠른 시작](quickstart.md)을 통해 첫 번째 텍스트 기반 에이전트를 구축합니다.
-   그런 다음 [에이전트 실행](running_agents.md#choose-a-memory-strategy)에서 턴 간 상태를 유지할 방법을 결정합니다.
-   작업이 실제 파일, 저장소 또는 에이전트별로 격리된 워크스페이스 상태에 의존한다면 [샌드박스 에이전트 빠른 시작](sandbox_agents.md)을 참조하세요.
-   핸드오프와 관리자 방식 오케스트레이션 중에서 선택하려면 [에이전트 오케스트레이션](multi_agent.md)을 참조하세요.

## 경로 선택 {#choose-your-path}

수행하려는 작업은 알고 있지만 해당 작업을 설명하는 페이지를 모르는 경우 이 표를 사용하세요.

| 목표 | 시작 지점 |
| --- | --- |
| 첫 번째 텍스트 에이전트를 구축하고 전체 실행 과정 확인 | [빠른 시작](quickstart.md) |
| 함수 도구, 호스티드 툴 또는 Agents as tools 추가 | [도구](tools.md) |
| 실제 격리된 워크스페이스에서 코딩, 검토 또는 문서 에이전트 실행 | [샌드박스 에이전트 빠른 시작](sandbox_agents.md) 및 [샌드박스 클라이언트](sandbox/clients.md) |
| 핸드오프와 관리자 방식 오케스트레이션 중 선택 | [에이전트 오케스트레이션](multi_agent.md) |
| 턴 간 메모리 유지 | [에이전트 실행](running_agents.md#choose-a-memory-strategy) 및 [세션](sessions/index.md) |
| OpenAI 모델, WebSocket 전송 또는 OpenAI 이외의 제공업체 사용 | [모델](models/index.md) |
| 출력, 실행 항목, 인터럽션(중단 처리) 및 재개 상태 검토 | [결과](results.md) |
| `gpt-realtime-2.1`를 사용하여 지연 시간이 짧은 음성 에이전트 구축 | [실시간 에이전트 빠른 시작](realtime/quickstart.md) 및 [실시간 전송](realtime/transport.md) |
| 음성 텍스트 변환 / 에이전트 / 텍스트 음성 변환 파이프라인 구축 | [음성 파이프라인 빠른 시작](voice/quickstart.md) |