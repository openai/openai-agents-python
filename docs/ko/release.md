---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는, 약간 수정된 시맨틱 버저닝을 따릅니다. 앞의 `0`은 SDK가 아직 빠르게 발전 중임을 나타냅니다. 각 구성 요소는 다음과 같이 증가시킵니다.

## 마이너(`Y`) 버전

베타로 표시되지 않은 공개 인터페이스의 **호환성이 깨지는 변경**이 있는 경우 마이너 버전 `Y`를 올립니다. 예를 들어 `0.0.x`에서 `0.1.x`로 이동할 때 호환성이 깨지는 변경이 포함될 수 있습니다.

호환성이 깨지는 변경을 원하지 않는다면 프로젝트에서 `0.0.x` 버전으로 고정하는 것을 권장합니다.

## 패치(`Z`) 버전

호환성이 깨지지 않는 변경의 경우 `Z`를 증가시킵니다.

- 버그 수정
- 새 기능
- 비공개 인터페이스 변경
- 베타 기능 업데이트

## 호환성이 깨지는 변경 로그

### 0.16.0

이 버전에서는 SDK 기본 모델이 `gpt-4.1`이 아니라 `gpt-5.4-mini`로 변경되었습니다. 이는 모델을 명시적으로 설정하지 않은 에이전트와 실행에 영향을 줍니다. 새 기본값이 GPT-5 모델이므로, 암시적 기본 모델 설정에는 이제 `reasoning.effort="none"` 및 `verbosity="low"` 같은 GPT-5 기본값이 포함됩니다.

이전 기본 모델 동작을 유지해야 하는 경우 에이전트 또는 실행 구성에서 모델을 명시적으로 설정하거나 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

주요 사항:

- `Runner.run`, `Runner.run_sync`, `Runner.run_streamed`는 이제 턴 제한을 비활성화하기 위해 `max_turns=None`을 허용합니다.
- 샌드박스 워크스페이스 하이드레이션은 이제 로컬, Docker, 공급자 기반 샌드박스 구현 전반에서 절대 심볼릭 링크 대상을 포함하여 아카이브 루트 밖을 가리키는 심볼릭 링크가 있는 tar 아카이브를 거부합니다.

### 0.15.0

이 버전에서는 모델 거부가 빈 텍스트 출력으로 처리되거나, structured outputs의 경우 실행 루프가 `MaxTurnsExceeded`까지 재시도하게 하는 대신, `ModelRefusalError`로 명시적으로 노출됩니다.

이는 이전에 거부만 포함된 모델 응답이 `final_output == ""`로 완료될 것으로 기대하던 코드에 영향을 줍니다. 예외를 발생시키지 않고 거부를 처리하려면 `model_refusal` 실행 오류 핸들러를 제공하세요.

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured-output 에이전트의 경우 핸들러는 에이전트의 출력 스키마와 일치하는 값을 반환할 수 있으며, SDK는 다른 실행 오류 핸들러의 최종 출력과 동일하게 이를 검증합니다.

### 0.14.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않지만**, 샌드박스 에이전트라는 주요 신규 베타 기능 영역과 이를 로컬, 컨테이너화, 호스티드 환경 전반에서 사용하는 데 필요한 런타임, 백엔드, 문서 지원을 추가합니다.

주요 사항:

- `SandboxAgent`, `Manifest`, `SandboxRunConfig`를 중심으로 하는 새로운 베타 샌드박스 런타임 표면을 추가하여, 에이전트가 파일, 디렉터리, Git 리포지토리, 마운트, 스냅샷, 재개 지원을 갖춘 영구 격리 워크스페이스 내부에서 작업할 수 있게 했습니다.
- `UnixLocalSandboxClient` 및 `DockerSandboxClient`를 통한 로컬 및 컨테이너화 개발용 샌드박스 실행 백엔드를 추가했으며, 선택적 extras를 통해 Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop, Vercel에 대한 호스티드 공급자 통합도 추가했습니다.
- 향후 실행에서 이전 실행의 교훈을 재사용할 수 있도록 샌드박스 메모리 지원을 추가했으며, 점진적 공개, 멀티턴 그룹화, 구성 가능한 격리 경계, S3 기반 워크플로를 포함한 영구 메모리 예제를 제공합니다.
- 로컬 및 합성 워크스페이스 항목, S3/R2/GCS/Azure Blob Storage/S3 Files용 원격 스토리지 마운트, 이식 가능한 스냅샷, `RunState`, `SandboxSessionState` 또는 저장된 스냅샷을 통한 재개 흐름을 포함하여 더 광범위한 워크스페이스 및 재개 모델을 추가했습니다.
- `examples/sandbox/` 아래에 스킬, 핸드오프, 메모리, 공급자별 설정을 활용한 코딩 작업과 코드 리뷰, 데이터룸 QA, 웹사이트 클로닝 같은 엔드투엔드 워크플로를 다루는 상당한 샌드박스 예제와 튜토리얼을 추가했습니다.
- 샌드박스 인식 세션 준비, 기능 바인딩, 상태 직렬화, 통합 트레이싱, 프롬프트 캐시 키 기본값, 더 안전한 민감한 MCP 출력 편집으로 코어 런타임 및 트레이싱 스택을 확장했습니다.

### 0.13.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않지만**, 주목할 만한 Realtime 기본값 업데이트와 새로운 MCP 기능 및 런타임 안정성 수정이 포함되어 있습니다.

주요 사항:

- 기본 websocket Realtime 모델은 이제 `gpt-realtime-1.5`이므로, 새로운 Realtime 에이전트 설정은 추가 구성 없이 최신 모델을 사용합니다.
- `MCPServer`는 이제 `list_resources()`, `list_resource_templates()`, `read_resource()`를 노출하고, `MCPServerStreamableHttp`는 이제 `session_id`를 노출하여 스트리밍 가능한 HTTP 세션을 재연결 또는 무상태 워커 간에 재개할 수 있습니다.
- Chat Completions 통합은 이제 `should_replay_reasoning_content`를 통해 reasoning-content 재생을 선택할 수 있어, LiteLLM/DeepSeek 같은 어댑터에서 공급자별 추론/도구 호출 연속성을 개선합니다.
- `SQLAlchemySession`의 동시 첫 쓰기, 추론 제거 후 고아 assistant 메시지 ID가 있는 컴팩션 요청, `remove_all_tools()`가 MCP/추론 항목을 남기는 문제, 함수 도구 배치 실행기의 레이스 등 여러 런타임 및 세션 엣지 케이스를 수정했습니다.

### 0.12.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)를 확인하세요.

### 0.11.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)를 확인하세요.

### 0.10.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않지만**, OpenAI Responses 사용자를 위한 중요한 신규 기능 영역인 Responses API의 websocket 전송 지원이 포함되어 있습니다.

주요 사항:

- OpenAI Responses 모델에 대한 websocket 전송 지원을 추가했습니다(옵트인, HTTP는 기본 전송으로 유지).
- 멀티턴 실행에서 공유 websocket 지원 공급자와 `RunConfig`를 재사용하기 위한 `responses_websocket_session()` 헬퍼 / `ResponsesWebSocketSession`을 추가했습니다.
- 스트리밍, tools, 승인, 후속 턴을 다루는 새로운 websocket 스트리밍 예제(`examples/basic/stream_ws.py`)를 추가했습니다.

### 0.9.0

이 버전에서는 이 주요 버전이 3개월 전에 EOL에 도달했으므로 Python 3.9가 더 이상 지원되지 않습니다. 더 새로운 런타임 버전으로 업그레이드하세요.

또한 `Agent#as_tool()` 메서드에서 반환되는 값의 타입 힌트가 `Tool`에서 `FunctionTool`로 좁혀졌습니다. 이 변경은 일반적으로 호환성 문제를 일으키지 않지만, 코드가 더 넓은 유니언 타입에 의존하는 경우 일부 조정이 필요할 수 있습니다.

### 0.8.0

이 버전에서는 두 가지 런타임 동작 변경으로 인해 마이그레이션 작업이 필요할 수 있습니다.

- **동기** Python 호출 가능 객체를 래핑하는 함수 도구는 이제 이벤트 루프 스레드에서 실행되는 대신 `asyncio.to_thread(...)`를 통해 워커 스레드에서 실행됩니다. 도구 로직이 스레드 로컬 상태 또는 스레드 종속 리소스에 의존한다면 비동기 도구 구현으로 마이그레이션하거나 도구 코드에서 스레드 종속성을 명시적으로 처리하세요.
- 로컬 MCP 도구 실패 처리는 이제 구성 가능하며, 기본 동작은 전체 실행을 실패시키는 대신 모델에 표시되는 오류 출력을 반환할 수 있습니다. fail-fast 의미 체계에 의존한다면 `mcp_config={"failure_error_function": None}`을 설정하세요. 서버 수준 `failure_error_function` 값은 에이전트 수준 설정을 재정의하므로, 명시적 핸들러가 있는 각 로컬 MCP 서버에 `failure_error_function=None`을 설정하세요.

### 0.7.0

이 버전에서는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작 변경이 있었습니다.

- 중첩 핸드오프 기록은 이제 **옵트인**입니다(기본적으로 비활성화). v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`를 명시적으로 설정하세요.
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`가 `"none"`으로 변경되었습니다(SDK 기본값에서 구성했던 이전 기본값 `"low"`에서 변경). 프롬프트 또는 품질/비용 프로필이 `"low"`에 의존했다면 `model_settings`에서 명시적으로 설정하세요.

### 0.6.0

이 버전에서는 기본 핸드오프 기록이 이제 원문 사용자/assistant 턴을 노출하는 대신 단일 assistant 메시지로 패키징되어, 다운스트림 에이전트에 간결하고 예측 가능한 요약을 제공합니다
- 기존 단일 메시지 핸드오프 대화 기록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하므로 다운스트림 에이전트는 명확하게 표시된 요약을 받습니다

### 0.5.0

이 버전은 눈에 보이는 호환성이 깨지는 변경을 도입하지는 않지만, 내부적으로 새 기능과 몇 가지 중요한 업데이트가 포함되어 있습니다.

- [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip)을 처리하기 위한 `RealtimeRunner` 지원을 추가했습니다.
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 크게 수정했습니다.

### 0.4.0

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전이 더 이상 지원되지 않습니다. 이 SDK와 함께 openai v2.x를 사용하세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델 및 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 예전에는 `Agent`를 인자로 받던 몇몇 곳이 이제 대신 `AgentBase`를 인자로 받습니다. 예를 들어 MCP 서버의 `list_tools()` 호출이 있습니다. 이는 순수하게 타이핑 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꿔 타입 오류만 수정하면 됩니다.

### 0.1.0

이 버전에서는 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 두 개의 새 매개변수 `run_context`와 `agent`가 추가되었습니다. `MCPServer`를 서브클래싱하는 모든 클래스에 이 매개변수를 추가해야 합니다.